package versionfs

import (
	"fmt"
	"strings"
	"testing"

	"lazymind/core/filediff"
)

func TestApplyTextReviewAcceptsAdaptiveHunksIndependently(t *testing.T) {
	oldLines := make([]string, 0, 7)
	newLines := make([]string, 0, 7)
	for index := 1; index <= 7; index++ {
		oldLines = append(oldLines, fmt.Sprintf("preference_%02d: common value a suffix", index))
		newLines = append(newLines, fmt.Sprintf("preference_%02d: common value b suffix", index))
	}
	headContent := strings.Join(oldLines, "\n") + "\n"
	draftContent := strings.Join(newLines, "\n") + "\n"
	diff, err := filediff.CompareContent(
		filediff.Content{Path: "memory/user.md", Data: []byte(headContent), EditableText: true},
		filediff.Content{Path: "memory/user.md", Data: []byte(draftContent), EditableText: true},
		filediff.Options{},
	)
	if err != nil {
		t.Fatalf("CompareContent returned error: %v", err)
	}

	file := ReviewFile{Path: diff.Path, Type: EntryTypeFile, Status: diff.Status}
	for _, line := range diff.DiffEntryLines {
		file.DiffLines = append(file.DiffLines, DiffLine{
			Type: line.Type, Text: line.Text, HTML: line.HTML,
			OldLine: line.OldLine, NewLine: line.NewLine,
			OldStart: line.OldStart, OldLines: line.OldLines,
			NewStart: line.NewStart, NewLines: line.NewLines,
		})
	}
	AnnotateReviewFile(&file, ReviewSessionMeta{ID: "review-1"}, nil, false)
	hunks := HunkLines(file)
	if len(hunks) != 2 {
		t.Fatalf("hunk count = %d, want 2", len(hunks))
	}

	decisions := map[string]string{
		DecisionKey(file.Path, hunks[0].HunkID): DecisionAccepted,
		DecisionKey(file.Path, hunks[1].HunkID): DecisionRejected,
	}
	got, err := ApplyTextReview(headContent, draftContent, file, decisions)
	if err != nil {
		t.Fatalf("ApplyTextReview returned error: %v", err)
	}
	wantLines := append(append([]string{}, newLines[:3]...), oldLines[3:]...)
	want := strings.Join(wantLines, "\n") + "\n"
	if got != want {
		t.Fatalf("merged content mismatch\ngot:\n%s\nwant:\n%s", got, want)
	}
}
