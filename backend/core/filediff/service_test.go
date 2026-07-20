package filediff

import (
	"fmt"
	"strings"
	"testing"
)

func TestCompareContentSplitsSeparatedChangeBlocksIntoHunks(t *testing.T) {
	oldText := "test123\nfirst\n\n\nsecond\n\n\n\nmiddle\n\n\nthird\n\n\ntail\n"
	newText := "test123\nfirst changed\n\n\nsecond changed\n\n\n\nmiddle\n\n\nthird changed\n\n\ntail\n"

	diff, err := CompareContent(
		Content{Path: "567", Data: []byte(oldText), EditableText: true, Size: int64(len(oldText))},
		Content{Path: "567", Data: []byte(newText), EditableText: true, Size: int64(len(newText))},
		Options{ContextLines: 3},
	)
	if err != nil {
		t.Fatalf("CompareContent returned error: %v", err)
	}
	if diff.HunkCount != 3 {
		t.Fatalf("HunkCount = %d, want 3; lines = %#v", diff.HunkCount, diff.DiffEntryLines)
	}

	hunks := hunkLines(diff.DiffEntryLines)
	if len(hunks) != 3 {
		t.Fatalf("hunk lines = %d, want 3", len(hunks))
	}
	if hunks[0].OldStart != 1 || hunks[0].OldLines != 4 || hunks[0].NewStart != 1 || hunks[0].NewLines != 4 {
		t.Fatalf("first hunk range mismatch: %#v", hunks[0])
	}
	if hunks[1].OldStart != 5 || hunks[1].OldLines != 7 || hunks[1].NewStart != 5 || hunks[1].NewLines != 7 {
		t.Fatalf("second hunk range mismatch: %#v", hunks[1])
	}
	if hunks[2].OldStart != 12 || hunks[2].OldLines != 4 || hunks[2].NewStart != 12 || hunks[2].NewLines != 4 {
		t.Fatalf("third hunk range mismatch: %#v", hunks[2])
	}
}

func hunkLines(lines []DiffEntryLine) []DiffEntryLine {
	out := []DiffEntryLine{}
	for _, line := range lines {
		if line.Type == "HUNK" {
			out = append(out, line)
		}
	}
	return out
}

func TestCompareContentAdaptivelySplitsContinuousChangesByEditWeight(t *testing.T) {
	tests := []struct {
		name       string
		lineCount  int
		oldValue   string
		newValue   string
		wantRanges []int
	}{
		{
			name:       "three light edits stay together",
			lineCount:  3,
			oldValue:   "common value a suffix",
			newValue:   "common value b suffix",
			wantRanges: []int{3},
		},
		{
			name:       "seven light edits split without a line-count threshold",
			lineCount:  7,
			oldValue:   "common value a suffix",
			newValue:   "common value b suffix",
			wantRanges: []int{3, 4},
		},
		{
			name:       "ten light edits form two balanced hunks",
			lineCount:  10,
			oldValue:   "common value a suffix",
			newValue:   "common value b suffix",
			wantRanges: []int{5, 5},
		},
		{
			name:       "ten medium edits form four hunks",
			lineCount:  10,
			oldValue:   "common abcde suffix",
			newValue:   "common vwxyz suffix",
			wantRanges: []int{2, 3, 2, 3},
		},
		{
			name:       "ten heavy edits form five hunks",
			lineCount:  10,
			oldValue:   "common 甲乙丙丁戊己庚辛壬癸子丑寅 suffix",
			newValue:   "common 天地玄黄宇宙洪荒日月盈昃辰 suffix",
			wantRanges: []int{2, 2, 2, 2, 2},
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			oldText, newText := continuousChangedLines(tt.lineCount, tt.oldValue, tt.newValue)
			diff, err := CompareContent(
				Content{Path: "memory.md", Data: []byte(oldText), EditableText: true},
				Content{Path: "memory.md", Data: []byte(newText), EditableText: true},
				Options{},
			)
			if err != nil {
				t.Fatalf("CompareContent returned error: %v", err)
			}

			hunks := hunkLines(diff.DiffEntryLines)
			if len(hunks) != len(tt.wantRanges) {
				t.Fatalf("hunk count = %d, want %d; lines = %#v", len(hunks), len(tt.wantRanges), diff.DiffEntryLines)
			}
			for index, wantLines := range tt.wantRanges {
				if hunks[index].OldLines != wantLines || hunks[index].NewLines != wantLines {
					t.Fatalf("hunk %d ranges = old:%d new:%d, want %d; header = %#v", index, hunks[index].OldLines, hunks[index].NewLines, wantLines, hunks[index])
				}
			}
		})
	}
}

func TestCompareContentPrefersNearbyDocumentBoundary(t *testing.T) {
	oldLines := []string{
		"plain line 1 a", "plain line 2 a", "plain line 3 a", "plain line 4 a",
		"plain line 5 a", "plain line 6 a", "plain line 7 a", "plain line 8 a",
	}
	newLines := []string{
		"plain line 1 b", "plain line 2 b", "plain line 3 b", "# New section b",
		"plain line 5 b", "plain line 6 b", "plain line 7 b", "plain line 8 b",
	}
	diff, err := CompareContent(
		Content{Path: "skill.md", Data: []byte(strings.Join(oldLines, "\n") + "\n"), EditableText: true},
		Content{Path: "skill.md", Data: []byte(strings.Join(newLines, "\n") + "\n"), EditableText: true},
		Options{},
	)
	if err != nil {
		t.Fatalf("CompareContent returned error: %v", err)
	}

	hunks := hunkLines(diff.DiffEntryLines)
	if len(hunks) != 2 {
		t.Fatalf("hunk count = %d, want 2; lines = %#v", len(hunks), diff.DiffEntryLines)
	}
	if hunks[0].OldLines != 3 || hunks[1].OldLines != 5 {
		t.Fatalf("hunk ranges = %d/%d, want 3/5; hunks = %#v", hunks[0].OldLines, hunks[1].OldLines, hunks)
	}
}

func continuousChangedLines(lineCount int, oldValue, newValue string) (string, string) {
	oldLines := make([]string, 0, lineCount)
	newLines := make([]string, 0, lineCount)
	for index := 1; index <= lineCount; index++ {
		oldLines = append(oldLines, fmt.Sprintf("preference_%02d: %s", index, oldValue))
		newLines = append(newLines, fmt.Sprintf("preference_%02d: %s", index, newValue))
	}
	return strings.Join(oldLines, "\n") + "\n", strings.Join(newLines, "\n") + "\n"
}
