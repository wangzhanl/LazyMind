package remotefs

import (
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"testing"

	"lazymind/core/common/orm"
)

func newRemoteFSTestDB(t *testing.T) *orm.DB {
	t.Helper()
	db, err := orm.Connect(orm.DriverSQLite, filepath.Join(t.TempDir(), "remotefs.db"))
	if err != nil {
		t.Fatalf("connect db: %v", err)
	}
	if err := db.AutoMigrate(
		&orm.PersonalResource{},
		&orm.PersonalResourceBlob{},
		&orm.PersonalResourceRevision{},
		&orm.PersonalResourceDraft{},
		&orm.PersonalResourceReviewSession{},
		&orm.PersonalResourceReviewActionBatch{},
		&orm.PersonalResourceReviewActionItem{},
	); err != nil {
		t.Fatalf("auto migrate: %v", err)
	}
	return db
}

func TestPersonalResourceTaskModes(t *testing.T) {
	db := newRemoteFSTestDB(t)
	handler := NewHandler(db.DB)

	writeReview := httptest.NewRequest(http.MethodPut, "/remote-fs/content?path=memory/memory.md&user_id=u1&task_id=review_1", strings.NewReader("review draft"))
	writeReviewRec := httptest.NewRecorder()
	handler.Content(writeReviewRec, writeReview)
	if writeReviewRec.Code != http.StatusOK {
		t.Fatalf("expected review write status 200, got %d body=%s", writeReviewRec.Code, writeReviewRec.Body.String())
	}

	readReview := httptest.NewRequest(http.MethodGet, "/remote-fs/content?path=memory/memory.md&user_id=u1&task_id=review_1", nil)
	readReviewRec := httptest.NewRecorder()
	handler.Content(readReviewRec, readReview)
	if readReviewRec.Code != http.StatusOK || readReviewRec.Body.String() != "review draft" {
		t.Fatalf("expected review read draft, got status=%d body=%q", readReviewRec.Code, readReviewRec.Body.String())
	}

	readEditor := httptest.NewRequest(http.MethodGet, "/remote-fs/content?path=memory/memory.md&user_id=u1&task_id=session_1", nil)
	readEditorRec := httptest.NewRecorder()
	handler.Content(readEditorRec, readEditor)
	if readEditorRec.Code != http.StatusOK || readEditorRec.Body.String() != "" {
		t.Fatalf("expected editor read head, got status=%d body=%q", readEditorRec.Code, readEditorRec.Body.String())
	}

	writeEditor := httptest.NewRequest(http.MethodPut, "/remote-fs/content?path=memory/memory.md&user_id=u1&task_id=session_1", strings.NewReader("editor draft"))
	writeEditorRec := httptest.NewRecorder()
	handler.Content(writeEditorRec, writeEditor)
	if writeEditorRec.Code != http.StatusConflict {
		t.Fatalf("expected editor write conflict, got %d body=%s", writeEditorRec.Code, writeEditorRec.Body.String())
	}
}
