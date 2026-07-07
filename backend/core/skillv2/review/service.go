package review

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"html"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/google/uuid"
	"gorm.io/gorm"

	skilldiff "lazymind/core/skillv2/diff"
	skillsearch "lazymind/core/skillv2/search"
	skillservice "lazymind/core/skillv2/service"
)

const (
	statusActive      = "active"
	statusInvalidated = "invalidated"
	statusCommitted   = "committed"
	statusDiscarded   = "discarded"

	decisionPending  = "pending"
	decisionAccepted = "accepted"
	decisionRejected = "rejected"

	defaultUndoLimit = 20
)

type ServiceDeps struct {
	DB        *gorm.DB
	BlobStore *skillservice.BlobStore
	UndoLimit int
}

type Service struct {
	db        *gorm.DB
	blobStore *skillservice.BlobStore
	undoLimit int
	clock     clock
}

type PrepareFileRequest struct {
	SkillID string
	UserID  string
	File    skilldiff.DiffFile
}

type ActionRequest struct {
	SkillID               string
	UserID                string
	ReviewID              string
	ExpectedReviewVersion int64
	Items                 []ActionItem
}

type ActionItem struct {
	Path     string
	HunkID   string
	Decision string
}

type ActionResponse struct {
	ReviewVersion int64
	BatchID       string
	CanUndo       bool
}

type UndoRequest struct {
	SkillID               string
	UserID                string
	ReviewID              string
	ExpectedReviewVersion int64
}

type UndoResponse struct {
	ReviewVersion int64
	UndoneBatchID string
	Items         []ActionItem
	CanUndo       bool
}

type CommitRequest struct {
	SkillID               string
	UserID                string
	ReviewID              string
	ExpectedReviewVersion int64
}

type CommitResponse struct {
	RevisionID string
	RevisionNo int64
}

type ReviewInfo struct {
	ReviewID          string
	ReviewVersion     int64
	DraftVersion      int64
	BaseRevisionID    string
	DraftSnapshotHash string
	CanUndo           bool
}

func NewService(deps ServiceDeps) *Service {
	undoLimit := deps.UndoLimit
	if undoLimit <= 0 {
		undoLimit = defaultUndoLimit
	}
	return &Service{db: deps.DB, blobStore: deps.BlobStore, undoLimit: undoLimit, clock: systemClock{}}
}

func (s *Service) PrepareFile(ctx context.Context, req PrepareFileRequest) (skilldiff.DiffFile, error) {
	session, err := s.ensureSession(ctx, s.db, req.SkillID, req.UserID)
	if err != nil {
		return skilldiff.DiffFile{}, err
	}
	decisions, err := s.currentDecisions(ctx, s.db, session.ID)
	if err != nil {
		return skilldiff.DiffFile{}, err
	}
	canUndo, err := s.canUndo(ctx, s.db, session.ID)
	if err != nil {
		return skilldiff.DiffFile{}, err
	}
	file := req.File
	s.annotateFile(&file, session, decisions, canUndo)
	return file, nil
}

func (s *Service) Action(ctx context.Context, req ActionRequest) (ActionResponse, error) {
	var out ActionResponse
	if req.ExpectedReviewVersion <= 0 {
		return out, fmt.Errorf("expected_review_version required")
	}
	if len(req.Items) == 0 {
		return out, fmt.Errorf("items required")
	}
	err := s.db.WithContext(ctx).Transaction(func(tx *gorm.DB) error {
		session, err := s.loadActiveSession(ctx, tx, req.SkillID, req.ReviewID)
		if err != nil {
			return err
		}
		if session.Version != req.ExpectedReviewVersion {
			return fmt.Errorf("stale review version")
		}
		if err := s.validateSnapshot(ctx, tx, session); err != nil {
			return err
		}
		known, err := s.hunksByPath(ctx, tx, session, uniquePaths(req.Items))
		if err != nil {
			return err
		}
		decisions, err := s.currentDecisions(ctx, tx, session.ID)
		if err != nil {
			return err
		}
		now := s.clock.Now()
		seq, err := nextBatchSequence(tx, session.ID)
		if err != nil {
			return err
		}
		batchID := uuid.NewString()
		createdBy := nullableString(req.UserID)
		if err := tx.Create(&reviewActionBatchRow{
			ID:              batchID,
			ReviewSessionID: session.ID,
			Sequence:        seq,
			CreatedBy:       createdBy,
			CreatedAt:       now,
		}).Error; err != nil {
			return err
		}
		rows := make([]reviewActionItemRow, 0, len(req.Items))
		for _, item := range req.Items {
			path := strings.TrimSpace(item.Path)
			hunkID := strings.TrimSpace(item.HunkID)
			decision := strings.TrimSpace(item.Decision)
			if decision != decisionAccepted && decision != decisionRejected {
				return fmt.Errorf("invalid decision")
			}
			if !known[path][hunkID] {
				return fmt.Errorf("unknown hunk_id")
			}
			before := decisions[decisionKey(path, hunkID)]
			if before == "" {
				before = decisionPending
			}
			rows = append(rows, reviewActionItemRow{
				ID:              uuid.NewString(),
				BatchID:         batchID,
				ReviewSessionID: session.ID,
				Path:            path,
				HunkID:          hunkID,
				BeforeDecision:  before,
				AfterDecision:   decision,
				CreatedAt:       now,
			})
			decisions[decisionKey(path, hunkID)] = decision
		}
		if err := tx.Create(&rows).Error; err != nil {
			return err
		}
		nextVersion := session.Version + 1
		if err := tx.Model(&reviewSessionRow{}).Where("id = ?", session.ID).Updates(map[string]any{
			"version":    nextVersion,
			"updated_by": nullableString(req.UserID),
			"updated_at": now,
		}).Error; err != nil {
			return err
		}
		if err := s.lockOldUndoBatches(tx, session.ID, seq); err != nil {
			return err
		}
		canUndo, err := s.canUndo(ctx, tx, session.ID)
		if err != nil {
			return err
		}
		out = ActionResponse{ReviewVersion: nextVersion, BatchID: batchID, CanUndo: canUndo}
		return nil
	})
	return out, err
}

func (s *Service) Undo(ctx context.Context, req UndoRequest) (UndoResponse, error) {
	var out UndoResponse
	if req.ExpectedReviewVersion <= 0 {
		return out, fmt.Errorf("expected_review_version required")
	}
	err := s.db.WithContext(ctx).Transaction(func(tx *gorm.DB) error {
		session, err := s.loadActiveSession(ctx, tx, req.SkillID, req.ReviewID)
		if err != nil {
			return err
		}
		if session.Version != req.ExpectedReviewVersion {
			return fmt.Errorf("stale review version")
		}
		if err := s.validateSnapshot(ctx, tx, session); err != nil {
			return err
		}
		var batch reviewActionBatchRow
		if err := tx.Where("review_session_id = ? AND undone_at IS NULL AND undo_locked = ?", session.ID, false).
			Order("sequence DESC").Take(&batch).Error; err != nil {
			if err == gorm.ErrRecordNotFound {
				return fmt.Errorf("undo not available")
			}
			return err
		}
		var items []reviewActionItemRow
		if err := tx.Where("batch_id = ?", batch.ID).Order("created_at ASC, id ASC").Find(&items).Error; err != nil {
			return err
		}
		now := s.clock.Now()
		if err := tx.Model(&reviewActionBatchRow{}).Where("id = ?", batch.ID).Updates(map[string]any{
			"undone_at": now,
			"undone_by": nullableString(req.UserID),
		}).Error; err != nil {
			return err
		}
		nextVersion := session.Version + 1
		if err := tx.Model(&reviewSessionRow{}).Where("id = ?", session.ID).Updates(map[string]any{
			"version":    nextVersion,
			"updated_by": nullableString(req.UserID),
			"updated_at": now,
		}).Error; err != nil {
			return err
		}
		outItems := make([]ActionItem, 0, len(items))
		for _, item := range items {
			outItems = append(outItems, ActionItem{Path: item.Path, HunkID: item.HunkID, Decision: item.BeforeDecision})
		}
		canUndo, err := s.canUndo(ctx, tx, session.ID)
		if err != nil {
			return err
		}
		out = UndoResponse{ReviewVersion: nextVersion, UndoneBatchID: batch.ID, Items: outItems, CanUndo: canUndo}
		return nil
	})
	return out, err
}

func (s *Service) Commit(ctx context.Context, req CommitRequest) (CommitResponse, error) {
	var out CommitResponse
	if req.ExpectedReviewVersion <= 0 {
		return out, fmt.Errorf("expected_review_version required")
	}
	err := s.db.WithContext(ctx).Transaction(func(tx *gorm.DB) error {
		session, err := s.loadActiveSession(ctx, tx, req.SkillID, req.ReviewID)
		if err != nil {
			return err
		}
		if session.Version != req.ExpectedReviewVersion {
			return fmt.Errorf("stale review version")
		}
		if err := s.validateSnapshot(ctx, tx, session); err != nil {
			return err
		}
		decisions, err := s.currentDecisions(ctx, tx, session.ID)
		if err != nil {
			return err
		}
		baseEntries, err := entriesForRevision(ctx, tx, req.SkillID, session.BaseRevisionID)
		if err != nil {
			return err
		}
		draftEntries, err := mergedEntriesForDraft(ctx, tx, req.SkillID, session.BaseRevisionID)
		if err != nil {
			return err
		}
		finalEntries, err := s.reviewedEntries(ctx, tx, session, baseEntries, draftEntries, decisions)
		if err != nil {
			return err
		}
		nextNo, err := nextRevisionNo(tx, req.SkillID)
		if err != nil {
			return err
		}
		revisionID := uuid.NewString()
		now := s.clock.Now()
		if err := tx.Create(&skillRevisionRow{
			ID:               revisionID,
			SkillID:          req.SkillID,
			ParentRevisionID: &session.BaseRevisionID,
			RevisionNo:       nextNo,
			TreeHash:         hashTree(finalEntries),
			ChangeSource:     "draft_review",
			CreatedBy:        nullableString(req.UserID),
			CreatedAt:        now,
		}).Error; err != nil {
			return err
		}
		if err := createRevisionEntries(tx, revisionID, finalEntries); err != nil {
			return err
		}
		if err := tx.Model(&skillRow{}).Where("id = ?", req.SkillID).Updates(map[string]any{
			"head_revision_id": revisionID,
			"version":          gorm.Expr("version + 1"),
			"updated_at":       now,
		}).Error; err != nil {
			return err
		}
		if err := tx.Where("skill_id = ?", req.SkillID).Delete(&skillDraftEntryRow{}).Error; err != nil {
			return err
		}
		if err := tx.Model(&skillDraftRow{}).Where("skill_id = ?", req.SkillID).Updates(map[string]any{
			"base_revision_id": revisionID,
			"task_id":          "",
			"conversation_id":  nil,
			"updated_by":       nullableString(req.UserID),
			"version":          gorm.Expr("version + 1"),
			"draft_updated_at": nil,
			"updated_at":       now,
		}).Error; err != nil {
			return err
		}
		if err := deleteReviewSession(ctx, tx, session.ID); err != nil {
			return err
		}
		if err := cleanupUnreferencedBlobs(ctx, tx, s.blobStore); err != nil {
			return err
		}
		if err := skillsearch.RebuildSkillTx(ctx, tx, req.SkillID, now); err != nil {
			return err
		}
		out = CommitResponse{RevisionID: revisionID, RevisionNo: nextNo}
		return nil
	})
	return out, err
}

func MarkSkillReviews(ctx context.Context, tx *gorm.DB, skillID, status, userID string, now time.Time) error {
	return markSkillReviews(ctx, tx, skillID, status, userID, now)
}

func (s *Service) ensureSession(ctx context.Context, db *gorm.DB, skillID, userID string) (reviewSessionRow, error) {
	baseRevisionID, draftVersion, snapshot, err := draftReviewSnapshot(ctx, db, skillID)
	if err != nil {
		return reviewSessionRow{}, err
	}
	var session reviewSessionRow
	err = db.WithContext(ctx).Where("skill_id = ? AND status = ?", skillID, statusActive).Order("updated_at DESC").Take(&session).Error
	now := s.clock.Now()
	if err == nil {
		if session.BaseRevisionID == baseRevisionID && session.DraftVersionAtStart == draftVersion && session.DraftSnapshotHash == snapshot {
			return session, nil
		}
		if err := db.WithContext(ctx).Model(&reviewSessionRow{}).Where("id = ?", session.ID).Updates(map[string]any{
			"status":     statusInvalidated,
			"updated_by": nullableString(userID),
			"updated_at": now,
		}).Error; err != nil {
			return reviewSessionRow{}, err
		}
	} else if err != gorm.ErrRecordNotFound {
		return reviewSessionRow{}, err
	}
	session = reviewSessionRow{
		ID:                  uuid.NewString(),
		SkillID:             skillID,
		BaseRevisionID:      baseRevisionID,
		DraftVersionAtStart: draftVersion,
		DraftSnapshotHash:   snapshot,
		Status:              statusActive,
		Version:             1,
		UndoLimit:           s.undoLimit,
		CreatedBy:           nullableString(userID),
		UpdatedBy:           nullableString(userID),
		CreatedAt:           now,
		UpdatedAt:           now,
	}
	if err := db.WithContext(ctx).Create(&session).Error; err != nil {
		return reviewSessionRow{}, err
	}
	return session, nil
}

func (s *Service) loadActiveSession(ctx context.Context, tx *gorm.DB, skillID, reviewID string) (reviewSessionRow, error) {
	var session reviewSessionRow
	if err := tx.WithContext(ctx).Where("id = ? AND skill_id = ? AND status = ?", reviewID, skillID, statusActive).Take(&session).Error; err != nil {
		return reviewSessionRow{}, err
	}
	return session, nil
}

func (s *Service) validateSnapshot(ctx context.Context, tx *gorm.DB, session reviewSessionRow) error {
	baseRevisionID, draftVersion, snapshot, err := draftReviewSnapshot(ctx, tx, session.SkillID)
	if err != nil {
		return err
	}
	if baseRevisionID != session.BaseRevisionID || draftVersion != session.DraftVersionAtStart || snapshot != session.DraftSnapshotHash {
		return fmt.Errorf("draft snapshot changed")
	}
	return nil
}

func (s *Service) reviewedEntries(ctx context.Context, tx *gorm.DB, session reviewSessionRow, baseEntries, draftEntries map[string]mergedEntry, decisions map[string]string) (map[string]mergedEntry, error) {
	finalEntries := cloneEntries(baseEntries)
	paths := unionEntryPaths(baseEntries, draftEntries)
	for _, path := range paths {
		baseEntry, baseOK := baseEntries[path]
		draftEntry, draftOK := draftEntries[path]
		if baseOK && draftOK && entrySignature(baseEntry) == entrySignature(draftEntry) {
			finalEntries[path] = draftEntry
			continue
		}
		if (baseOK && baseEntry.EntryType == "dir") || (draftOK && draftEntry.EntryType == "dir") {
			if draftOK {
				finalEntries[path] = draftEntry
			} else {
				delete(finalEntries, path)
			}
			continue
		}
		file, err := s.diffFileForPath(ctx, tx, session, path)
		if err != nil {
			return nil, err
		}
		hunks := hunkLines(file)
		if len(hunks) == 0 {
			continue
		}
		fileDecision := ""
		for _, hunk := range hunks {
			decision := decisions[decisionKey(path, hunk.HunkID)]
			if decision == "" || decision == decisionPending {
				return nil, fmt.Errorf("pending hunks exist")
			}
			if fileDecision == "" {
				fileDecision = decision
			} else if fileDecision != decision {
				merged, err := s.mergeTextFile(ctx, tx, path, file, decisions)
				if err != nil {
					return nil, err
				}
				finalEntries[path] = merged
				fileDecision = ""
				break
			}
		}
		switch fileDecision {
		case decisionAccepted:
			if draftOK {
				finalEntries[path] = draftEntry
			} else {
				delete(finalEntries, path)
			}
		case decisionRejected:
			if baseOK {
				finalEntries[path] = baseEntry
			} else {
				delete(finalEntries, path)
			}
		case "":
		default:
			return nil, fmt.Errorf("invalid decision")
		}
	}
	return finalEntries, nil
}

func (s *Service) mergeTextFile(ctx context.Context, tx *gorm.DB, path string, file skilldiff.DiffFile, decisions map[string]string) (mergedEntry, error) {
	if file.Binary || file.TooLarge {
		return mergedEntry{}, fmt.Errorf("cannot merge hunks")
	}
	var out []string
	currentDecision := decisionPending
	for _, line := range file.DiffEntryLines {
		switch line.Type {
		case "HUNK":
			currentDecision = decisions[decisionKey(path, line.HunkID)]
			if currentDecision == "" || currentDecision == decisionPending {
				return mergedEntry{}, fmt.Errorf("pending hunks exist")
			}
		case "CONTEXT":
			out = append(out, line.Text)
		case "DELETION":
			if currentDecision == decisionRejected {
				out = append(out, line.Text)
			}
		case "ADDITION":
			if currentDecision == decisionAccepted {
				out = append(out, line.Text)
			}
		}
	}
	content := strings.Join(out, "\n")
	if len(out) > 0 {
		content += "\n"
	}
	blob, err := s.blobStore.Put(ctx, tx, path, []byte(content), s.clock)
	if err != nil {
		return mergedEntry{}, err
	}
	hash := blob.Hash
	return mergedEntry{Path: path, EntryType: "file", BlobHash: &hash, Size: blob.Size, Mime: blob.Mime, FileType: blob.FileType, Binary: blob.Binary, Mode: 0o644}, nil
}

func (s *Service) hunksByPath(ctx context.Context, tx *gorm.DB, session reviewSessionRow, paths []string) (map[string]map[string]bool, error) {
	out := map[string]map[string]bool{}
	for _, path := range paths {
		file, err := s.diffFileForPath(ctx, tx, session, path)
		if err != nil {
			return nil, err
		}
		out[path] = map[string]bool{}
		for _, hunk := range hunkLines(file) {
			out[path][hunk.HunkID] = true
		}
	}
	return out, nil
}

func (s *Service) diffFileForPath(ctx context.Context, tx *gorm.DB, session reviewSessionRow, path string) (skilldiff.DiffFile, error) {
	baseEntries, err := entriesForRevision(ctx, tx, session.SkillID, session.BaseRevisionID)
	if err != nil {
		return skilldiff.DiffFile{}, err
	}
	draftEntries, err := mergedEntriesForDraft(ctx, tx, session.SkillID, session.BaseRevisionID)
	if err != nil {
		return skilldiff.DiffFile{}, err
	}
	oldFS := dbReadOnlyFS{db: tx, entries: baseEntries}
	newFS := dbReadOnlyFS{db: tx, entries: draftEntries}
	file, err := skilldiff.NewService(skilldiff.ServiceDeps{}).CompareFile(ctx, oldFS, newFS, skilldiff.DiffOptions{Path: path})
	if err != nil {
		return skilldiff.DiffFile{}, err
	}
	s.annotateFile(&file, session, map[string]string{}, false)
	return file, nil
}

func (s *Service) annotateFile(file *skilldiff.DiffFile, session reviewSessionRow, decisions map[string]string, canUndo bool) {
	ensureSyntheticHunk(file)
	hunkIndex := 0
	for i := range file.DiffEntryLines {
		if file.DiffEntryLines[i].Type != "HUNK" {
			continue
		}
		hunkIndex++
		end := len(file.DiffEntryLines)
		for j := i + 1; j < len(file.DiffEntryLines); j++ {
			if file.DiffEntryLines[j].Type == "HUNK" {
				end = j
				break
			}
		}
		id, oldStart, oldLines, newStart, newLines := hunkID(session.ID, file.Path, file.Status, hunkIndex, file.DiffEntryLines[i:end])
		decision := decisions[decisionKey(file.Path, id)]
		if decision == "" {
			decision = decisionPending
		}
		file.DiffEntryLines[i].HunkID = id
		file.DiffEntryLines[i].Decision = decision
		file.DiffEntryLines[i].OldStart = oldStart
		file.DiffEntryLines[i].OldLines = oldLines
		file.DiffEntryLines[i].NewStart = newStart
		file.DiffEntryLines[i].NewLines = newLines
	}
	file.ReviewID = session.ID
	file.ReviewVersion = session.Version
	file.DraftVersion = session.DraftVersionAtStart
	file.BaseRevisionID = session.BaseRevisionID
	file.DraftSnapshotHash = session.DraftSnapshotHash
	file.CanUndo = canUndo
	for _, line := range file.DiffEntryLines {
		if line.Type != "HUNK" {
			continue
		}
		file.HunkCount++
		switch line.Decision {
		case decisionAccepted:
			file.AcceptedCount++
		case decisionRejected:
			file.RejectedCount++
		default:
			file.PendingCount++
		}
	}
}

func ensureSyntheticHunk(file *skilldiff.DiffFile) {
	if len(file.DiffEntryLines) > 0 || file.Type != "file" || file.Status == "unchanged" {
		return
	}
	text := "@@ file " + file.Status + " @@"
	file.DiffEntryLines = []skilldiff.DiffEntryLine{{
		Type:     "HUNK",
		Text:     text,
		HTML:     html.EscapeString(text),
		OldLine:  1,
		NewLine:  1,
		OldStart: 1,
		NewStart: 1,
	}}
}

func hunkID(sessionID, path, status string, index int, lines []skilldiff.DiffEntryLine) (string, int, int, int, int) {
	oldStart, newStart := 0, 0
	oldLines, newLines := 0, 0
	var deleted, added []string
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
			deleted = append(deleted, line.Text)
		case "ADDITION":
			if line.NewLine > 0 {
				if newStart == 0 {
					newStart = line.NewLine
				}
				newLines++
			}
			added = append(added, line.Text)
		case "HUNK":
			if line.OldLine > 0 && oldStart == 0 {
				oldStart = line.OldLine
			}
			if line.NewLine > 0 && newStart == 0 {
				newStart = line.NewLine
			}
		}
	}
	if oldStart == 0 {
		oldStart = 1
	}
	if newStart == 0 {
		newStart = 1
	}
	sum := sha256.Sum256([]byte(strings.Join([]string{
		sessionID,
		path,
		status,
		strconv.Itoa(index),
		strconv.Itoa(oldStart),
		strconv.Itoa(newStart),
		hashStrings(deleted),
		hashStrings(added),
	}, "\x00")))
	return "hunk_" + fmt.Sprintf("%04d", index) + "_" + hex.EncodeToString(sum[:])[:12], oldStart, oldLines, newStart, newLines
}

func hunkLines(file skilldiff.DiffFile) []skilldiff.DiffEntryLine {
	out := []skilldiff.DiffEntryLine{}
	for _, line := range file.DiffEntryLines {
		if line.Type == "HUNK" {
			out = append(out, line)
		}
	}
	return out
}

func draftReviewSnapshot(ctx context.Context, db *gorm.DB, skillID string) (string, int64, string, error) {
	var draft skillDraftRow
	if err := db.WithContext(ctx).Where("skill_id = ?", skillID).Take(&draft).Error; err != nil {
		return "", 0, "", err
	}
	var overlays []skillDraftEntryRow
	if err := db.WithContext(ctx).Where("skill_id = ?", skillID).Order("path ASC").Find(&overlays).Error; err != nil {
		return "", 0, "", err
	}
	if len(overlays) == 0 {
		return "", 0, "", fmt.Errorf("draft overlay is empty")
	}
	baseRevisionID := ""
	if draft.BaseRevisionID != nil {
		baseRevisionID = *draft.BaseRevisionID
	}
	if baseRevisionID == "" {
		var skill skillRow
		if err := db.WithContext(ctx).Where("id = ?", skillID).Take(&skill).Error; err != nil {
			return "", 0, "", err
		}
		if skill.HeadRevisionID == nil || *skill.HeadRevisionID == "" {
			return "", 0, "", fmt.Errorf("skill has no base revision")
		}
		baseRevisionID = *skill.HeadRevisionID
	}
	lines := make([]string, 0, len(overlays))
	for _, overlay := range overlays {
		hash := ""
		if overlay.BlobHash != nil {
			hash = *overlay.BlobHash
		}
		lines = append(lines, strings.Join([]string{
			overlay.Path,
			overlay.Op,
			overlay.EntryType,
			hash,
			strconv.FormatInt(overlay.Size, 10),
			overlay.FileType,
			strconv.FormatBool(overlay.Binary),
		}, "\x00"))
	}
	sum := sha256.Sum256([]byte(strings.Join(lines, "\n")))
	return baseRevisionID, draft.Version, hex.EncodeToString(sum[:]), nil
}

func (s *Service) currentDecisions(ctx context.Context, db *gorm.DB, reviewID string) (map[string]string, error) {
	var batches []reviewActionBatchRow
	if err := db.WithContext(ctx).Where("review_session_id = ? AND undone_at IS NULL", reviewID).Order("sequence ASC").Find(&batches).Error; err != nil {
		return nil, err
	}
	out := map[string]string{}
	for _, batch := range batches {
		var items []reviewActionItemRow
		if err := db.WithContext(ctx).Where("batch_id = ?", batch.ID).Order("created_at ASC, id ASC").Find(&items).Error; err != nil {
			return nil, err
		}
		for _, item := range items {
			out[decisionKey(item.Path, item.HunkID)] = item.AfterDecision
		}
	}
	return out, nil
}

func (s *Service) canUndo(ctx context.Context, db *gorm.DB, reviewID string) (bool, error) {
	var count int64
	if err := db.WithContext(ctx).Model(&reviewActionBatchRow{}).Where("review_session_id = ? AND undone_at IS NULL AND undo_locked = ?", reviewID, false).Count(&count).Error; err != nil {
		return false, err
	}
	return count > 0, nil
}

func (s *Service) lockOldUndoBatches(tx *gorm.DB, reviewID string, latestSeq int64) error {
	limit := s.undoLimit
	if limit <= 0 {
		return nil
	}
	cutoff := latestSeq - int64(limit)
	if cutoff <= 0 {
		return nil
	}
	return tx.Model(&reviewActionBatchRow{}).Where("review_session_id = ? AND sequence <= ?", reviewID, cutoff).Update("undo_locked", true).Error
}

type dbReadOnlyFS struct {
	db      *gorm.DB
	entries map[string]mergedEntry
}

func (fs dbReadOnlyFS) ListAll(ctx context.Context) ([]skilldiff.EntryInfo, error) {
	paths := make([]string, 0, len(fs.entries))
	for path := range fs.entries {
		paths = append(paths, path)
	}
	sort.Strings(paths)
	out := make([]skilldiff.EntryInfo, 0, len(paths))
	for _, path := range paths {
		entry := fs.entries[path]
		hash := ""
		if entry.BlobHash != nil {
			hash = *entry.BlobHash
		}
		out = append(out, skilldiff.EntryInfo{Path: path, Type: entry.EntryType, BlobHash: hash, Binary: entry.Binary, FileType: entry.FileType, Size: entry.Size})
	}
	return out, nil
}

func (fs dbReadOnlyFS) ReadFile(ctx context.Context, path string) ([]byte, error) {
	entry, ok := fs.entries[path]
	if !ok || entry.EntryType != "file" || entry.BlobHash == nil {
		return nil, fmt.Errorf("file not found: %s", path)
	}
	var blob skillBlobRow
	if err := fs.db.WithContext(ctx).Where("hash = ?", *entry.BlobHash).Take(&blob).Error; err != nil {
		return nil, err
	}
	if blob.Binary {
		return nil, fmt.Errorf("binary content is not available: %s", path)
	}
	return blob.Content, nil
}

func entriesForRevision(ctx context.Context, db *gorm.DB, skillID, revisionID string) (map[string]mergedEntry, error) {
	var rev skillRevisionRow
	if err := db.WithContext(ctx).Where("id = ? AND skill_id = ?", revisionID, skillID).Take(&rev).Error; err != nil {
		return nil, err
	}
	var rows []skillRevisionEntryRow
	if err := db.WithContext(ctx).Where("revision_id = ?", revisionID).Order("path ASC").Find(&rows).Error; err != nil {
		return nil, err
	}
	out := make(map[string]mergedEntry, len(rows))
	for _, row := range rows {
		hash := row.BlobHash
		out[row.Path] = mergedEntry{Path: row.Path, EntryType: row.EntryType, BlobHash: hash, Size: row.Size, Mime: row.Mime, FileType: row.FileType, Binary: row.Binary, Mode: row.Mode}
	}
	return out, nil
}

func mergedEntriesForDraft(ctx context.Context, db *gorm.DB, skillID, baseRevisionID string) (map[string]mergedEntry, error) {
	entries, err := entriesForRevision(ctx, db, skillID, baseRevisionID)
	if err != nil {
		return nil, err
	}
	var overlays []skillDraftEntryRow
	if err := db.WithContext(ctx).Where("skill_id = ?", skillID).Order("path ASC").Find(&overlays).Error; err != nil {
		return nil, err
	}
	for _, overlay := range overlays {
		if overlay.Op == "delete" {
			for path := range entries {
				if path == overlay.Path || isDescendantPath(overlay.Path, path) {
					delete(entries, path)
				}
			}
			continue
		}
		hash := overlay.BlobHash
		entries[overlay.Path] = mergedEntry{Path: overlay.Path, EntryType: overlay.EntryType, BlobHash: hash, Size: overlay.Size, Mime: overlay.Mime, FileType: overlay.FileType, Binary: overlay.Binary, Mode: overlay.Mode}
	}
	return entries, nil
}

func createRevisionEntries(tx *gorm.DB, revisionID string, entries map[string]mergedEntry) error {
	rows := make([]skillRevisionEntryRow, 0, len(entries))
	for _, entry := range sortedEntries(entries) {
		hash := entry.BlobHash
		rows = append(rows, skillRevisionEntryRow{
			RevisionID: revisionID,
			Path:       entry.Path,
			EntryType:  entry.EntryType,
			BlobHash:   hash,
			Size:       entry.Size,
			Mime:       entry.Mime,
			FileType:   entry.FileType,
			Binary:     entry.Binary,
			Mode:       entry.Mode,
		})
	}
	if len(rows) == 0 {
		return nil
	}
	return tx.Create(&rows).Error
}

func cleanupUnreferencedBlobs(ctx context.Context, tx *gorm.DB, blobStore *skillservice.BlobStore) error {
	var blobs []skillBlobRow
	if err := tx.Find(&blobs).Error; err != nil {
		return err
	}
	for _, blob := range blobs {
		referenced, err := blobReferenced(tx, blob.Hash)
		if err != nil {
			return err
		}
		if referenced {
			continue
		}
		if err := blobStore.DeleteBlob(ctx, tx, blob.Hash); err != nil {
			return err
		}
	}
	return nil
}

func blobReferenced(tx *gorm.DB, hash string) (bool, error) {
	var revisionRefs int64
	if err := tx.Model(&skillRevisionEntryRow{}).Where("blob_hash = ?", hash).Count(&revisionRefs).Error; err != nil {
		return false, err
	}
	if revisionRefs > 0 {
		return true, nil
	}
	var draftRefs int64
	if err := tx.Model(&skillDraftEntryRow{}).Where("blob_hash = ?", hash).Count(&draftRefs).Error; err != nil {
		return false, err
	}
	return draftRefs > 0, nil
}

func markSkillReviews(ctx context.Context, tx *gorm.DB, skillID, status, userID string, now time.Time) error {
	updates := map[string]any{
		"status":     status,
		"updated_by": nullableString(userID),
		"updated_at": now,
	}
	return tx.WithContext(ctx).Model(&reviewSessionRow{}).Where("skill_id = ? AND status = ?", skillID, statusActive).Updates(updates).Error
}

func deleteReviewSession(ctx context.Context, tx *gorm.DB, reviewID string) error {
	if err := tx.WithContext(ctx).Where("review_session_id = ?", reviewID).Delete(&reviewActionItemRow{}).Error; err != nil {
		return err
	}
	if err := tx.WithContext(ctx).Where("review_session_id = ?", reviewID).Delete(&reviewActionBatchRow{}).Error; err != nil {
		return err
	}
	return tx.WithContext(ctx).Where("id = ?", reviewID).Delete(&reviewSessionRow{}).Error
}

func nextBatchSequence(tx *gorm.DB, reviewID string) (int64, error) {
	var maxSeq int64
	if err := tx.Model(&reviewActionBatchRow{}).Where("review_session_id = ?", reviewID).Select("COALESCE(MAX(sequence), 0)").Scan(&maxSeq).Error; err != nil {
		return 0, err
	}
	return maxSeq + 1, nil
}

func nextRevisionNo(tx *gorm.DB, skillID string) (int64, error) {
	var maxNo int64
	if err := tx.Model(&skillRevisionRow{}).Where("skill_id = ?", skillID).Select("COALESCE(MAX(revision_no), 0)").Scan(&maxNo).Error; err != nil {
		return 0, err
	}
	return maxNo + 1, nil
}

func hashTree(entries map[string]mergedEntry) string {
	lines := make([]string, 0, len(entries))
	for _, entry := range sortedEntries(entries) {
		hash := ""
		if entry.BlobHash != nil {
			hash = *entry.BlobHash
		}
		lines = append(lines, entry.Path+"\x00"+entry.EntryType+"\x00"+hash)
	}
	sum := sha256.Sum256([]byte(strings.Join(lines, "\n")))
	return hex.EncodeToString(sum[:])
}

func sortedEntries(entries map[string]mergedEntry) []mergedEntry {
	out := make([]mergedEntry, 0, len(entries))
	for _, entry := range entries {
		out = append(out, entry)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Path < out[j].Path })
	return out
}

func cloneEntries(entries map[string]mergedEntry) map[string]mergedEntry {
	out := make(map[string]mergedEntry, len(entries))
	for path, entry := range entries {
		out[path] = entry
	}
	return out
}

func unionEntryPaths(a, b map[string]mergedEntry) []string {
	seen := map[string]bool{}
	for path := range a {
		seen[path] = true
	}
	for path := range b {
		seen[path] = true
	}
	paths := make([]string, 0, len(seen))
	for path := range seen {
		paths = append(paths, path)
	}
	sort.Strings(paths)
	return paths
}

func uniquePaths(items []ActionItem) []string {
	seen := map[string]bool{}
	for _, item := range items {
		path := strings.TrimSpace(item.Path)
		if path != "" {
			seen[path] = true
		}
	}
	paths := make([]string, 0, len(seen))
	for path := range seen {
		paths = append(paths, path)
	}
	sort.Strings(paths)
	return paths
}

func entrySignature(entry mergedEntry) string {
	hash := ""
	if entry.BlobHash != nil {
		hash = *entry.BlobHash
	}
	return strings.Join([]string{entry.EntryType, hash, entry.FileType}, "\x00")
}

func decisionKey(path, hunkID string) string {
	return path + "\x00" + hunkID
}

func hashStrings(values []string) string {
	sum := sha256.Sum256([]byte(strings.Join(values, "\n")))
	return hex.EncodeToString(sum[:])
}

func nullableString(v string) *string {
	if strings.TrimSpace(v) == "" {
		return nil
	}
	return &v
}

func isDescendantPath(parent, candidate string) bool {
	return strings.HasPrefix(candidate, parent+"/")
}

type clock interface {
	Now() time.Time
}

type systemClock struct{}

func (systemClock) Now() time.Time { return time.Now() }

type reviewSessionRow struct {
	ID                  string    `gorm:"column:id;type:varchar(36);primaryKey"`
	SkillID             string    `gorm:"column:skill_id;type:varchar(36);not null"`
	BaseRevisionID      string    `gorm:"column:base_revision_id;type:varchar(36);not null"`
	DraftVersionAtStart int64     `gorm:"column:draft_version_at_start;not null"`
	DraftSnapshotHash   string    `gorm:"column:draft_snapshot_hash;type:text;not null"`
	Status              string    `gorm:"column:status;type:text;not null;default:'active'"`
	Version             int64     `gorm:"column:version;not null;default:1"`
	UndoLimit           int       `gorm:"column:undo_limit;not null;default:20"`
	CreatedBy           *string   `gorm:"column:created_by;type:text"`
	UpdatedBy           *string   `gorm:"column:updated_by;type:text"`
	CreatedAt           time.Time `gorm:"column:created_at;not null"`
	UpdatedAt           time.Time `gorm:"column:updated_at;not null"`
}

func (reviewSessionRow) TableName() string { return "skill_draft_review_sessions" }

type reviewActionBatchRow struct {
	ID              string     `gorm:"column:id;type:varchar(36);primaryKey"`
	ReviewSessionID string     `gorm:"column:review_session_id;type:varchar(36);not null"`
	Sequence        int64      `gorm:"column:sequence;not null"`
	UndoLocked      bool       `gorm:"column:undo_locked;not null;default:false"`
	UndoneAt        *time.Time `gorm:"column:undone_at"`
	UndoneBy        *string    `gorm:"column:undone_by;type:text"`
	CreatedBy       *string    `gorm:"column:created_by;type:text"`
	CreatedAt       time.Time  `gorm:"column:created_at;not null"`
}

func (reviewActionBatchRow) TableName() string {
	return "skill_draft_review_action_batches"
}

type reviewActionItemRow struct {
	ID              string    `gorm:"column:id;type:varchar(36);primaryKey"`
	BatchID         string    `gorm:"column:batch_id;type:varchar(36);not null"`
	ReviewSessionID string    `gorm:"column:review_session_id;type:varchar(36);not null"`
	Path            string    `gorm:"column:path;type:text;not null"`
	HunkID          string    `gorm:"column:hunk_id;type:text;not null"`
	BeforeDecision  string    `gorm:"column:before_decision;type:text;not null;default:'pending'"`
	AfterDecision   string    `gorm:"column:after_decision;type:text;not null"`
	CreatedAt       time.Time `gorm:"column:created_at;not null"`
}

func (reviewActionItemRow) TableName() string {
	return "skill_draft_review_action_items"
}

type skillRow struct {
	ID             string    `gorm:"column:id;type:varchar(36);primaryKey"`
	HeadRevisionID *string   `gorm:"column:head_revision_id;type:varchar(36)"`
	Version        int64     `gorm:"column:version"`
	UpdatedAt      time.Time `gorm:"column:updated_at"`
}

func (skillRow) TableName() string { return "skills" }

type skillBlobRow struct {
	Hash    string `gorm:"column:hash;type:text;primaryKey"`
	Binary  bool   `gorm:"column:binary"`
	Content []byte `gorm:"column:content;type:blob"`
}

func (skillBlobRow) TableName() string { return "skill_blobs" }

type skillRevisionRow struct {
	ID               string    `gorm:"column:id;type:varchar(36);primaryKey"`
	SkillID          string    `gorm:"column:skill_id;type:varchar(36);not null"`
	ParentRevisionID *string   `gorm:"column:parent_revision_id;type:varchar(36)"`
	RevisionNo       int64     `gorm:"column:revision_no;not null"`
	TreeHash         string    `gorm:"column:tree_hash;type:text;not null"`
	ChangeSource     string    `gorm:"column:change_source;type:text;not null;default:'draft_commit'"`
	CreatedBy        *string   `gorm:"column:created_by;type:text"`
	CreatedAt        time.Time `gorm:"column:created_at;not null"`
}

func (skillRevisionRow) TableName() string { return "skill_revisions" }

type skillRevisionEntryRow struct {
	RevisionID string  `gorm:"column:revision_id;type:varchar(36);primaryKey"`
	Path       string  `gorm:"column:path;type:text;primaryKey"`
	EntryType  string  `gorm:"column:entry_type;type:text;not null"`
	BlobHash   *string `gorm:"column:blob_hash;type:text"`
	Size       int64   `gorm:"column:size"`
	Mime       string  `gorm:"column:mime;type:text"`
	FileType   string  `gorm:"column:file_type;type:text"`
	Binary     bool    `gorm:"column:binary"`
	Mode       int     `gorm:"column:mode"`
}

func (skillRevisionEntryRow) TableName() string { return "skill_revision_entries" }

type skillDraftRow struct {
	SkillID        string     `gorm:"column:skill_id;type:varchar(36);primaryKey"`
	BaseRevisionID *string    `gorm:"column:base_revision_id;type:varchar(36)"`
	TaskID         string     `gorm:"column:task_id;type:text"`
	ConversationID *string    `gorm:"column:conversation_id;type:varchar(36)"`
	UpdatedBy      *string    `gorm:"column:updated_by;type:text"`
	Version        int64      `gorm:"column:version"`
	DraftUpdatedAt *time.Time `gorm:"column:draft_updated_at"`
	UpdatedAt      time.Time  `gorm:"column:updated_at"`
}

func (skillDraftRow) TableName() string { return "skill_drafts" }

type skillDraftEntryRow struct {
	SkillID   string  `gorm:"column:skill_id;type:varchar(36);primaryKey"`
	Path      string  `gorm:"column:path;type:text;primaryKey"`
	Op        string  `gorm:"column:op;type:text;not null"`
	EntryType string  `gorm:"column:entry_type;type:text"`
	BlobHash  *string `gorm:"column:blob_hash;type:text"`
	Size      int64   `gorm:"column:size"`
	Mime      string  `gorm:"column:mime;type:text"`
	FileType  string  `gorm:"column:file_type;type:text"`
	Binary    bool    `gorm:"column:binary"`
	Mode      int     `gorm:"column:mode"`
}

func (skillDraftEntryRow) TableName() string { return "skill_draft_entries" }

type mergedEntry struct {
	Path      string
	EntryType string
	BlobHash  *string
	Size      int64
	Mime      string
	FileType  string
	Binary    bool
	Mode      int
}
