package modelprovider

import (
	"testing"
	"time"

	"github.com/glebarez/sqlite"
	"gorm.io/gorm"

	"lazymind/core/common/orm"
)

func TestLoadSelectedModelsIncludesMaxInputTokens(t *testing.T) {
	dbName := "selected_models_" + time.Now().Format("150405.000000000")
	db, err := gorm.Open(sqlite.Open("file:"+dbName+"?mode=memory&cache=shared"), &gorm.Config{})
	if err != nil {
		t.Fatalf("open sqlite: %v", err)
	}
	if err := db.AutoMigrate(
		&orm.UserModelProvider{},
		&orm.UserModelProviderGroup{},
		&orm.UserModelProviderGroupModel{},
		&orm.UserSelectedModel{},
	); err != nil {
		t.Fatalf("migrate: %v", err)
	}

	now := time.Now().UTC()
	llmMaxInputTokens := "128K"
	vlmMaxInputTokens := "256K"
	if err := db.Create(&orm.UserModelProvider{
		ID:           "provider-1",
		Name:         "Qwen",
		Capabilities: "has_models",
		BaseModel:    orm.BaseModel{CreateUserID: "user-1", CreatedAt: now, UpdatedAt: now},
	}).Error; err != nil {
		t.Fatalf("create provider: %v", err)
	}
	if err := db.Create(&orm.UserModelProviderGroup{
		ID:                  "group-1",
		UserModelProviderID: "provider-1",
		Name:                "default",
		BaseURL:             "https://example.com/v1/",
		IsVerified:          true,
		BaseModel:           orm.BaseModel{CreateUserID: "user-1", CreatedAt: now, UpdatedAt: now},
	}).Error; err != nil {
		t.Fatalf("create group: %v", err)
	}
	models := []orm.UserModelProviderGroupModel{
		{
			ID:                       "llm-model",
			UserModelProviderID:      "provider-1",
			UserModelProviderGroupID: "group-1",
			ProviderName:             "Qwen",
			Name:                     "qwen3-8b",
			ModelType:                "llm",
			MaxInputTokens:           &llmMaxInputTokens,
			IsDefault:                true,
			BaseModel:                orm.BaseModel{CreateUserID: "user-1", CreatedAt: now, UpdatedAt: now},
		},
		{
			ID:                       "embed-model",
			UserModelProviderID:      "provider-1",
			UserModelProviderGroupID: "group-1",
			ProviderName:             "Qwen",
			Name:                     "text-embedding-v4",
			ModelType:                "embed",
			IsDefault:                true,
			BaseModel:                orm.BaseModel{CreateUserID: "user-1", CreatedAt: now, UpdatedAt: now},
		},
		{
			ID:                       "vlm-model",
			UserModelProviderID:      "provider-1",
			UserModelProviderGroupID: "group-1",
			ProviderName:             "Qwen",
			Name:                     "qwen3-vl-plus",
			ModelType:                "vlm",
			MaxInputTokens:           &vlmMaxInputTokens,
			IsDefault:                true,
			BaseModel:                orm.BaseModel{CreateUserID: "user-1", CreatedAt: now, UpdatedAt: now},
		},
	}
	if err := db.Create(&models).Error; err != nil {
		t.Fatalf("create models: %v", err)
	}
	selections := []orm.UserSelectedModel{
		{UserID: "user-1", ModelKey: "llm", UserModelProviderGroupModelID: "llm-model", CreatedAt: now, UpdatedAt: now},
		{UserID: "user-1", ModelKey: "embed_main", UserModelProviderGroupModelID: "embed-model", CreatedAt: now, UpdatedAt: now},
		{UserID: "user-1", ModelKey: "vlm", UserModelProviderGroupModelID: "vlm-model", CreatedAt: now, UpdatedAt: now},
	}
	if err := db.Create(&selections).Error; err != nil {
		t.Fatalf("create selections: %v", err)
	}

	got, err := loadSelectedModels(t.Context(), db, "user-1")
	if err != nil {
		t.Fatalf("load selected models: %v", err)
	}
	if len(got) != 3 {
		t.Fatalf("selected model count = %d, want 3", len(got))
	}
	if got[0].ModelKey != "embed_main" || got[0].MaxInputTokens != nil {
		t.Fatalf("embedding selection = %#v, want null max_input_tokens", got[0])
	}
	if got[1].ModelKey != "llm" || got[1].MaxInputTokens == nil || *got[1].MaxInputTokens != "128K" {
		t.Fatalf("llm selection = %#v, want max_input_tokens 128K", got[1])
	}
	if got[2].ModelKey != "vlm" || got[2].MaxInputTokens == nil || *got[2].MaxInputTokens != "256K" {
		t.Fatalf("vlm selection = %#v, want max_input_tokens 256K", got[2])
	}
}
