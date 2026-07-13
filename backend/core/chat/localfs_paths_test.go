package chat

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"reflect"
	"testing"
	"time"

	"lazymind/core/common/orm"
)

func TestApplyLocalFSPathsForChatAddsActiveLocalBindings(t *testing.T) {
	db, err := orm.Connect(orm.DriverSQLite, t.TempDir()+"/localfs-chat.db")
	if err != nil {
		t.Fatalf("connect db: %v", err)
	}
	if err := db.AutoMigrate(&orm.LocalFSChatSetting{}); err != nil {
		t.Fatalf("auto migrate: %v", err)
	}
	now := time.Now()
	if err := db.Create(&orm.LocalFSChatSetting{
		CreateUserID:   "u1",
		CreateUserName: "User 1",
		Enabled:        true,
		CreatedAt:      now,
		UpdatedAt:      now,
	}).Error; err != nil {
		t.Fatalf("create setting: %v", err)
	}

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if got := r.Header.Get("X-User-ID"); got != "u1" {
			t.Errorf("unexpected X-User-ID: %q", got)
		}
		w.Header().Set("Content-Type", "application/json")
		switch r.URL.Path {
		case "/api/scan/sources":
			_ = json.NewEncoder(w).Encode(map[string]any{
				"items": []map[string]any{
					{"source_id": "source-1", "status": "ACTIVE", "dataset_id": "ds-1", "tenant_id": "", "chat_enabled": true},
					{"source_id": "source-2", "status": "ACTIVE", "dataset_id": "ds-2", "tenant_id": "", "chat_enabled": true},
				},
				"total": 2,
			})
		case "/api/scan/sources/source-1":
			_ = json.NewEncoder(w).Encode(map[string]any{
				"bindings": []map[string]any{
					{"connector_type": "local_fs", "target_type": "local_path", "target_ref": "/tmp/a", "status": "ACTIVE"},
					{"connector_type": "feishu", "target_type": "wiki_node", "target_ref": "wiki:space:node", "status": "ACTIVE"},
				},
			})
		case "/api/scan/sources/source-2":
			_ = json.NewEncoder(w).Encode(map[string]any{
				"bindings": []map[string]any{
					{"connector_type": "local_fs", "target_type": "local_path", "target_ref": "/tmp/a", "status": "ACTIVE", "include_extensions": []string{"pdf"}},
					{"connector_type": "local_fs", "target_type": "local_path", "target_ref": "/tmp/b", "status": "ACTIVE", "include_extensions": []string{"doc", "docx"}},
					{"connector_type": "local_fs", "target_type": "local_path", "target_ref": "/tmp/paused", "status": "PAUSED", "include_extensions": []string{"txt"}},
				},
			})
		default:
			http.NotFound(w, r)
		}
	}))
	t.Cleanup(server.Close)
	t.Setenv("LAZYMIND_SCAN_CONTROL_PLANE_URL", server.URL)

	req := httptest.NewRequest(http.MethodPost, "/api/core/conversations:chat", nil)
	req.Header.Set("X-User-Id", "u1")
	reqBody := map[string]any{}
	if err := applyLocalFSPathsForChat(req.Context(), req, db.DB, "u1", reqBody); err != nil {
		t.Fatalf("apply local fs paths: %v", err)
	}

	got, ok := reqBody["local_fs_sources"].([]any)
	if !ok {
		t.Fatalf("local_fs_sources missing or wrong type: %#v", reqBody["local_fs_sources"])
	}
	// source-1: chat_enabled=true but no include_extensions on local_fs bindings → filtered out.
	// source-2: chat_enabled=true, one active local_fs binding with extensions.
	want := []map[string]any{
		{"source_id": "source-2", "paths": []any{"/var/lib/lazymind/uploads/tenants/root/datasets/ds-2/docs/files/"}, "file_extensions": []any{"doc", "docx", "pdf"}},
	}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("local_fs_sources = %#v, want %#v", got, want)
	}
}

func TestApplyLocalFSPathsForChatDisabledOmitsParameter(t *testing.T) {
	db, err := orm.Connect(orm.DriverSQLite, t.TempDir()+"/localfs-chat-disabled.db")
	if err != nil {
		t.Fatalf("connect db: %v", err)
	}
	if err := db.AutoMigrate(&orm.LocalFSChatSetting{}); err != nil {
		t.Fatalf("auto migrate: %v", err)
	}

	req := httptest.NewRequest(http.MethodPost, "/api/core/conversations:chat", nil)
	req.Header.Set("X-User-Id", "u1")
	reqBody := map[string]any{}
	if err := applyLocalFSPathsForChat(req.Context(), req, db.DB, "u1", reqBody); err != nil {
		t.Fatalf("apply local fs paths: %v", err)
	}
	if _, ok := reqBody["local_fs_sources"]; ok {
		t.Fatalf("local_fs_sources should be omitted when setting is disabled: %#v", reqBody)
	}
}

func TestApplyLocalFSPathsForChatFiltersChatDisabled(t *testing.T) {
	db, err := orm.Connect(orm.DriverSQLite, t.TempDir()+"/localfs-chat-filter.db")
	if err != nil {
		t.Fatalf("connect db: %v", err)
	}
	if err := db.AutoMigrate(&orm.LocalFSChatSetting{}); err != nil {
		t.Fatalf("auto migrate: %v", err)
	}
	now := time.Now()
	if err := db.Create(&orm.LocalFSChatSetting{
		CreateUserID:   "u1",
		CreateUserName: "User 1",
		Enabled:        true,
		CreatedAt:      now,
		UpdatedAt:      now,
	}).Error; err != nil {
		t.Fatalf("create setting: %v", err)
	}

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		switch r.URL.Path {
		case "/api/scan/sources":
			_ = json.NewEncoder(w).Encode(map[string]any{
				"items": []map[string]any{
					{"source_id": "source-1", "status": "ACTIVE", "dataset_id": "ds-1", "tenant_id": "", "chat_enabled": true},
					{"source_id": "source-2", "status": "ACTIVE", "dataset_id": "ds-2", "tenant_id": "", "chat_enabled": false},
				},
				"total": 2,
			})
		case "/api/scan/sources/source-1":
			_ = json.NewEncoder(w).Encode(map[string]any{
				"bindings": []map[string]any{
					{"connector_type": "local_fs", "target_type": "local_path", "target_ref": "/container/enabled", "status": "ACTIVE", "include_extensions": []string{"pdf"}},
				},
			})
		case "/api/scan/sources/source-2":
			_ = json.NewEncoder(w).Encode(map[string]any{
				"bindings": []map[string]any{
					{"connector_type": "local_fs", "target_type": "local_path", "target_ref": "/container/disabled", "status": "ACTIVE", "include_extensions": []string{"pdf"}},
				},
			})
		default:
			http.NotFound(w, r)
		}
	}))
	t.Cleanup(server.Close)
	t.Setenv("LAZYMIND_SCAN_CONTROL_PLANE_URL", server.URL)

	req := httptest.NewRequest(http.MethodPost, "/api/core/conversations:chat", nil)
	req.Header.Set("X-User-Id", "u1")
	reqBody := map[string]any{}
	if err := applyLocalFSPathsForChat(req.Context(), req, db.DB, "u1", reqBody); err != nil {
		t.Fatalf("apply local fs paths: %v", err)
	}

	got, ok := reqBody["local_fs_sources"].([]any)
	if !ok {
		t.Fatalf("local_fs_sources missing or wrong type: %#v", reqBody)
	}
	// Only source-1 (chat_enabled=true) should appear.
	if len(got) != 1 {
		t.Fatalf("expected 1 source, got %d: %#v", len(got), got)
	}
	entry := got[0].(map[string]any)
	if entry["source_id"] != "source-1" {
		t.Fatalf("expected source-1, got %v", entry["source_id"])
	}
	paths := entry["paths"].([]any)
	if len(paths) != 1 || paths[0].(string) != "/var/lib/lazymind/uploads/tenants/root/datasets/ds-1/docs/files/" {
		t.Fatalf("unexpected paths: %#v", paths)
	}
}
