package filediff

import (
	"fmt"
	"html"
	"strings"
	"unicode/utf8"
)

const defaultMaxTextBytes = 512 * 1024
const maxInlineDiffCells = 20000
const targetHunkEditWeight = 6

func CompareContent(old Content, next Content, opts Options) (FileDiff, error) {
	maxBytes := defaultMaxTextBytes
	result := FileDiff{
		Path:         firstNonEmpty(next.Path, old.Path),
		Status:       "modified",
		Binary:       old.Binary || next.Binary,
		EditableText: old.EditableText && next.EditableText,
		Supported:    true,
	}
	if string(old.Data) == string(next.Data) && old.Binary == next.Binary {
		result.Status = "unchanged"
	}
	if old.Size == 0 {
		old.Size = int64(len(old.Data))
	}
	if next.Size == 0 {
		next.Size = int64(len(next.Data))
	}
	if result.Binary {
		result.Supported = false
		result.UnsupportedReason = "binary content is not supported"
		return result, nil
	}
	if !result.EditableText {
		result.Supported = false
		result.UnsupportedReason = "content is not editable text"
		return result, nil
	}
	if old.Size > int64(maxBytes) || next.Size > int64(maxBytes) {
		result.Supported = false
		result.UnsupportedReason = "content is too large"
		result.TooLarge = true
		return result, nil
	}
	if !utf8.Valid(old.Data) || !utf8.Valid(next.Data) {
		result.Supported = false
		result.UnsupportedReason = "content is not valid utf-8"
		result.Binary = true
		return result, nil
	}
	if strings.EqualFold(opts.Mode, "context") {
		text := injectedContextText(string(next.Data), opts)
		result.DiffEntryLines = []DiffEntryLine{{
			Type:    "INJECTED_CONTEXT",
			Text:    text,
			HTML:    html.EscapeString(text),
			NewLine: opts.NewStart,
			OldLine: opts.OldStart,
		}}
		return result, nil
	}
	result.DiffEntryLines = buildTextDiff(string(old.Data), string(next.Data))
	result.HunkCount = countHunks(result.DiffEntryLines)
	return result, nil
}

func buildTextDiff(oldText, newText string) []DiffEntryLine {
	oldLines, oldHasFinalNewline := splitLines(oldText)
	newLines, newHasFinalNewline := splitLines(newText)
	lines := []DiffEntryLine{}
	oldLine, newLine := 1, 1
	for len(oldLines) > 0 || len(newLines) > 0 {
		switch {
		case len(oldLines) > 0 && len(newLines) > 0 && oldLines[0] == newLines[0]:
			lines = append(lines, diffLine("CONTEXT", oldLines[0], oldLine, newLine))
			oldLines = oldLines[1:]
			newLines = newLines[1:]
			oldLine++
			newLine++
		case len(oldLines) > 0 && len(newLines) > 0:
			deletion, addition := changedLinePair(oldLines[0], newLines[0], oldLine, newLine)
			lines = append(lines, deletion, addition)
			oldLines = oldLines[1:]
			newLines = newLines[1:]
			oldLine++
			newLine++
		case len(oldLines) > 0:
			lines = append(lines, highlightedDiffLine("DELETION", oldLines[0], oldLine, 0, "diff-deletion"))
			oldLines = oldLines[1:]
			oldLine++
		default:
			lines = append(lines, highlightedDiffLine("ADDITION", newLines[0], 0, newLine, "diff-addition"))
			newLines = newLines[1:]
			newLine++
		}
	}
	if oldHasFinalNewline != newHasFinalNewline && len(lines) > 0 {
		lines[len(lines)-1].DisplayNoNewLineWarning = true
	}
	return insertHunkHeaders(lines)
}

func splitLines(text string) ([]string, bool) {
	if text == "" {
		return nil, true
	}
	hasFinalNewline := strings.HasSuffix(text, "\n")
	parts := strings.Split(strings.TrimSuffix(text, "\n"), "\n")
	return parts, hasFinalNewline
}

func diffLine(typ, text string, oldLine, newLine int) DiffEntryLine {
	return DiffEntryLine{
		Type:    typ,
		Text:    text,
		HTML:    html.EscapeString(text),
		OldLine: oldLine,
		NewLine: newLine,
	}
}

func highlightedDiffLine(typ, text string, oldLine, newLine int, class string) DiffEntryLine {
	line := diffLine(typ, text, oldLine, newLine)
	line.HTML = wrapDiffSpan(class, html.EscapeString(text))
	if typ == "DELETION" {
		line.editWeight = editWeight(text, "")
	} else {
		line.editWeight = editWeight("", text)
	}
	return line
}

func changedLinePair(oldText, newText string, oldLine, newLine int) (DiffEntryLine, DiffEntryLine) {
	oldHTML, newHTML, weight := inlineDiffHTML(oldText, newText)
	deletion := diffLine("DELETION", oldText, oldLine, 0)
	addition := diffLine("ADDITION", newText, 0, newLine)
	deletion.HTML = oldHTML
	addition.HTML = newHTML
	deletion.editWeight = weight
	addition.editWeight = weight
	return deletion, addition
}

func inlineDiffHTML(oldText, newText string) (string, string, int) {
	oldRunes := []rune(oldText)
	newRunes := []rune(newText)
	if len(oldRunes) == 0 {
		return "", wrapDiffSpan("diff-addition", html.EscapeString(newText)), editWeightFromCounts(0, len(newRunes), 0, len(newRunes))
	}
	if len(newRunes) == 0 {
		return wrapDiffSpan("diff-deletion", html.EscapeString(oldText)), "", editWeightFromCounts(len(oldRunes), 0, len(oldRunes), 0)
	}
	if len(oldRunes)*len(newRunes) > maxInlineDiffCells {
		oldChanged, newChanged := changedRuneCounts(oldRunes, newRunes)
		return wrapDiffSpan("diff-deletion", html.EscapeString(oldText)), wrapDiffSpan("diff-addition", html.EscapeString(newText)), editWeightFromCounts(oldChanged, newChanged, len(oldRunes), len(newRunes))
	}

	oldChanged, newChanged := changedRuneMasks(oldRunes, newRunes)
	oldChangedCount := countChangedRunes(oldChanged)
	newChangedCount := countChangedRunes(newChanged)
	return renderInlineDiffHTML(oldRunes, oldChanged, "diff-deletion"), renderInlineDiffHTML(newRunes, newChanged, "diff-addition"), editWeightFromCounts(oldChangedCount, newChangedCount, len(oldRunes), len(newRunes))
}

func changedRuneMasks(oldRunes, newRunes []rune) ([]bool, []bool) {
	m, n := len(oldRunes), len(newRunes)
	lcs := make([][]int, m+1)
	for i := range lcs {
		lcs[i] = make([]int, n+1)
	}
	for i := m - 1; i >= 0; i-- {
		for j := n - 1; j >= 0; j-- {
			if oldRunes[i] == newRunes[j] {
				lcs[i][j] = lcs[i+1][j+1] + 1
				continue
			}
			if lcs[i+1][j] >= lcs[i][j+1] {
				lcs[i][j] = lcs[i+1][j]
			} else {
				lcs[i][j] = lcs[i][j+1]
			}
		}
	}

	oldChanged := make([]bool, m)
	newChanged := make([]bool, n)
	for i := range oldChanged {
		oldChanged[i] = true
	}
	for i := range newChanged {
		newChanged[i] = true
	}
	for i, j := 0, 0; i < m && j < n; {
		if oldRunes[i] == newRunes[j] {
			oldChanged[i] = false
			newChanged[j] = false
			i++
			j++
			continue
		}
		if lcs[i+1][j] >= lcs[i][j+1] {
			i++
		} else {
			j++
		}
	}
	return oldChanged, newChanged
}

func renderInlineDiffHTML(runes []rune, changed []bool, class string) string {
	var builder strings.Builder
	for i := 0; i < len(runes); {
		j := i + 1
		for j < len(runes) && changed[j] == changed[i] {
			j++
		}
		escaped := html.EscapeString(string(runes[i:j]))
		if changed[i] {
			builder.WriteString(wrapDiffSpan(class, escaped))
		} else {
			builder.WriteString(escaped)
		}
		i = j
	}
	return builder.String()
}

func wrapDiffSpan(class, escapedText string) string {
	return `<span class="` + class + `">` + escapedText + `</span>`
}

func insertHunkHeaders(lines []DiffEntryLine) []DiffEntryLine {
	hunkStarts := adaptiveHunkStarts(lines)
	if len(hunkStarts) == 0 {
		return nil
	}

	starts := make(map[int]bool, len(hunkStarts))
	for _, index := range hunkStarts {
		starts[index] = true
	}
	out := make([]DiffEntryLine, 0, len(lines)+len(hunkStarts))
	hunkIndex := 0
	for index, line := range lines {
		if starts[index] {
			hunkIndex++
			out = append(out, newHunkHeader(hunkIndex))
		}
		out = append(out, line)
	}
	updateHunkHeaders(out)
	return out
}

type diffEditUnit struct {
	start  int
	weight int
	text   string
}

func adaptiveHunkStarts(lines []DiffEntryLine) []int {
	hasChange := false
	for _, line := range lines {
		if startsDiffHunk(line) {
			hasChange = true
			break
		}
	}
	if !hasChange {
		return nil
	}

	starts := []int{0}
	seenChangedRun := false
	for index := 0; index < len(lines); {
		if !startsDiffHunk(lines[index]) {
			index++
			continue
		}
		runStart := index
		for index < len(lines) && startsDiffHunk(lines[index]) {
			index++
		}
		runEnd := index
		if seenChangedRun {
			starts = append(starts, runStart)
		}
		units := buildDiffEditUnits(lines, runStart, runEnd)
		for _, cut := range balancedEditUnitCuts(units) {
			starts = append(starts, units[cut].start)
		}
		seenChangedRun = true
	}
	return starts
}

func buildDiffEditUnits(lines []DiffEntryLine, start, end int) []diffEditUnit {
	units := make([]diffEditUnit, 0, end-start)
	for index := start; index < end; {
		unitEnd := index + 1
		if lines[index].Type == "DELETION" && unitEnd < end && lines[unitEnd].Type == "ADDITION" {
			unitEnd++
		}
		units = append(units, newDiffEditUnit(lines, index, unitEnd))
		index = unitEnd
	}
	return units
}

func newDiffEditUnit(lines []DiffEntryLine, start, end int) diffEditUnit {
	unit := diffEditUnit{start: start}
	var oldText, newText string
	for _, line := range lines[start:end] {
		if line.editWeight > unit.weight {
			unit.weight = line.editWeight
		}
		switch line.Type {
		case "DELETION":
			oldText = line.Text
		case "ADDITION":
			newText = line.Text
		default:
			if unit.text == "" {
				unit.text = line.Text
			}
		}
	}
	if newText != "" {
		unit.text = newText
	} else if oldText != "" {
		unit.text = oldText
	}
	if unit.weight == 0 {
		unit.weight = editWeight(oldText, newText)
	}
	return unit
}

func editWeight(oldText, newText string) int {
	oldRunes := []rune(oldText)
	newRunes := []rune(newText)
	oldChanged, newChanged := changedRuneCounts(oldRunes, newRunes)
	return editWeightFromCounts(oldChanged, newChanged, len(oldRunes), len(newRunes))
}

func editWeightFromCounts(oldChanged, newChanged, oldLength, newLength int) int {
	changed := oldChanged
	if newChanged > changed {
		changed = newChanged
	}
	longer := oldLength
	if newLength > longer {
		longer = newLength
	}

	weight := 1
	if changed > 4 {
		weight++
	}
	if changed > 12 {
		weight++
	}
	if changed > 24 || changed > 4 && longer > 0 && changed*10 >= longer*8 {
		weight++
	}
	if weight > 4 {
		return 4
	}
	return weight
}

func changedRuneCounts(oldRunes, newRunes []rune) (int, int) {
	if len(oldRunes) == 0 || len(newRunes) == 0 {
		return len(oldRunes), len(newRunes)
	}
	if len(oldRunes)*len(newRunes) <= maxInlineDiffCells {
		oldChanged, newChanged := changedRuneMasks(oldRunes, newRunes)
		return countChangedRunes(oldChanged), countChangedRunes(newChanged)
	}

	prefix := 0
	for prefix < len(oldRunes) && prefix < len(newRunes) && oldRunes[prefix] == newRunes[prefix] {
		prefix++
	}
	suffix := 0
	for suffix < len(oldRunes)-prefix && suffix < len(newRunes)-prefix && oldRunes[len(oldRunes)-1-suffix] == newRunes[len(newRunes)-1-suffix] {
		suffix++
	}
	return len(oldRunes) - prefix - suffix, len(newRunes) - prefix - suffix
}

func countChangedRunes(changed []bool) int {
	count := 0
	for _, isChanged := range changed {
		if isChanged {
			count++
		}
	}
	return count
}

func balancedEditUnitCuts(units []diffEditUnit) []int {
	if len(units) < 2 {
		return nil
	}
	prefixWeights := make([]int, len(units)+1)
	for index, unit := range units {
		prefixWeights[index+1] = prefixWeights[index] + unit.weight
	}
	totalWeight := prefixWeights[len(units)]
	blockCount := (totalWeight + targetHunkEditWeight - 1) / targetHunkEditWeight
	if blockCount > len(units) {
		blockCount = len(units)
	}
	if blockCount <= 1 {
		return nil
	}

	cuts := make([]int, 0, blockCount-1)
	previousCut := 0
	searchAt := 1
	for part := 1; part < blockCount; part++ {
		minCut := previousCut + 1
		maxCut := len(units) - (blockCount - part)
		targetNumerator := totalWeight * part
		for searchAt < minCut {
			searchAt++
		}
		for searchAt < maxCut && prefixWeights[searchAt]*blockCount < targetNumerator {
			searchAt++
		}

		candidateStart := searchAt - 2
		if candidateStart < minCut {
			candidateStart = minCut
		}
		candidateEnd := searchAt + 2
		if candidateEnd > maxCut {
			candidateEnd = maxCut
		}
		bestCut := candidateStart
		bestScore := int(^uint(0) >> 1)
		for candidate := candidateStart; candidate <= candidateEnd; candidate++ {
			distance := prefixWeights[candidate]*blockCount - targetNumerator
			if distance < 0 {
				distance = -distance
			}
			score := distance*4 - structuralBoundaryScore(units[candidate-1].text, units[candidate].text)*blockCount
			blockWeight := prefixWeights[candidate] - prefixWeights[previousCut]
			if blockWeight < 2 && !singleHeavyUnit(units, previousCut, candidate) {
				score += 8 * blockCount
			}
			if part == blockCount-1 {
				tailWeight := totalWeight - prefixWeights[candidate]
				if tailWeight < 2 && !singleHeavyUnit(units, candidate, len(units)) {
					score += 8 * blockCount
				}
			}
			if score < bestScore {
				bestScore = score
				bestCut = candidate
			}
		}
		cuts = append(cuts, bestCut)
		previousCut = bestCut
		searchAt = bestCut + 1
	}
	return cuts
}

func singleHeavyUnit(units []diffEditUnit, start, end int) bool {
	return end-start == 1 && units[start].weight == 4
}

func structuralBoundaryScore(previous, next string) int {
	previousTrimmed := strings.TrimSpace(previous)
	nextTrimmed := strings.TrimSpace(next)
	if previousTrimmed == "" || nextTrimmed == "" {
		return 4
	}
	if isMarkdownHeading(nextTrimmed) {
		return 4
	}
	if isTopLevelField(next) {
		return 3
	}
	if isListItem(nextTrimmed) {
		return 2
	}
	return 0
}

func isMarkdownHeading(text string) bool {
	for index := 0; index < len(text) && text[index] == '#'; index++ {
		if index+1 < len(text) && text[index+1] == ' ' {
			return true
		}
	}
	return false
}

func isTopLevelField(text string) bool {
	if text == "" || text != strings.TrimLeft(text, " \t") || strings.HasPrefix(text, "-") || strings.HasPrefix(text, "{") || strings.HasPrefix(text, "[") {
		return false
	}
	colon := strings.IndexByte(text, ':')
	if colon <= 0 {
		return false
	}
	return !strings.ContainsAny(text[:colon], " \t")
}

func isListItem(text string) bool {
	if strings.HasPrefix(text, "- ") || strings.HasPrefix(text, "* ") || strings.HasPrefix(text, "+ ") {
		return true
	}
	index := 0
	for index < len(text) && text[index] >= '0' && text[index] <= '9' {
		index++
	}
	return index > 0 && index+1 < len(text) && (text[index] == '.' || text[index] == ')') && text[index+1] == ' '
}

func newHunkHeader(index int) DiffEntryLine {
	return DiffEntryLine{Type: "HUNK", HunkID: fmt.Sprintf("hunk_%d", index)}
}

func isChangedDiffLine(line DiffEntryLine) bool {
	return line.Type == "ADDITION" || line.Type == "DELETION"
}

func startsDiffHunk(line DiffEntryLine) bool {
	return isChangedDiffLine(line) || line.DisplayNoNewLineWarning
}

func updateHunkHeaders(lines []DiffEntryLine) {
	for i := range lines {
		if lines[i].Type != "HUNK" {
			continue
		}
		end := len(lines)
		for j := i + 1; j < len(lines); j++ {
			if lines[j].Type == "HUNK" {
				end = j
				break
			}
		}
		oldStart, oldLines, newStart, newLines := hunkRange(lines[i+1 : end])
		lines[i].OldStart = oldStart
		lines[i].OldLines = oldLines
		lines[i].NewStart = newStart
		lines[i].NewLines = newLines
		lines[i].Text = formatHunkHeader(oldStart, oldLines, newStart, newLines)
		lines[i].HTML = html.EscapeString(lines[i].Text)
	}
}

func hunkRange(lines []DiffEntryLine) (int, int, int, int) {
	oldStart, newStart := 0, 0
	oldLines, newLines := 0, 0
	for _, line := range lines {
		switch line.Type {
		case "CONTEXT":
			if line.OldLine > 0 {
				if oldStart == 0 {
					oldStart = line.OldLine
				}
				oldLines++
			}
			if line.NewLine > 0 {
				if newStart == 0 {
					newStart = line.NewLine
				}
				newLines++
			}
		case "DELETION":
			if line.OldLine > 0 {
				if oldStart == 0 {
					oldStart = line.OldLine
				}
				oldLines++
			}
		case "ADDITION":
			if line.NewLine > 0 {
				if newStart == 0 {
					newStart = line.NewLine
				}
				newLines++
			}
		}
	}
	if oldStart == 0 {
		oldStart = 1
	}
	if newStart == 0 {
		newStart = 1
	}
	return oldStart, oldLines, newStart, newLines
}

func formatHunkHeader(oldStart, oldLines, newStart, newLines int) string {
	return fmt.Sprintf("@@ -%s +%s @@", formatHunkRange(oldStart, oldLines), formatHunkRange(newStart, newLines))
}

func formatHunkRange(start, lines int) string {
	if lines == 1 {
		return fmt.Sprintf("%d", start)
	}
	return fmt.Sprintf("%d,%d", start, lines)
}

func injectedContextText(text string, opts Options) string {
	lines, _ := splitLines(text)
	if len(lines) == 0 {
		return ""
	}
	start := opts.NewStart
	if start <= 0 {
		start = 1
	}
	count := opts.Lines
	if count <= 0 {
		count = 1
	}
	start--
	if start >= len(lines) {
		return ""
	}
	end := start + count
	if end > len(lines) {
		end = len(lines)
	}
	return strings.Join(lines[start:end], "\n")
}

func countHunks(lines []DiffEntryLine) int {
	count := 0
	for _, line := range lines {
		if strings.EqualFold(line.Type, "HUNK") {
			count++
		}
	}
	return count
}

func firstNonEmpty(values ...string) string {
	for _, value := range values {
		if strings.TrimSpace(value) != "" {
			return value
		}
	}
	return ""
}
