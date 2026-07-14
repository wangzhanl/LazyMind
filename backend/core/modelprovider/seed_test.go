package modelprovider

import (
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/glebarez/sqlite"
	"gorm.io/gorm"

	"lazymind/core/common/orm"
)

func TestModelCatalogMaxInputTokensOnlyForLLMAndVLMModels(t *testing.T) {
	yamlBytes, err := os.ReadFile(filepath.Join("..", "config", "model_catalog.yaml"))
	if err != nil {
		t.Fatalf("read model catalog: %v", err)
	}
	catalog, err := loadModelCatalog(yamlBytes)
	if err != nil {
		t.Fatalf("parse model catalog: %v", err)
	}

	limitCount := 0
	for _, section := range catalog {
		for _, supplier := range section.Suppliers {
			for _, model := range supplier.Models {
				if model.Type != "llm" && model.Type != "vlm" {
					if model.MaxInputTokens == nil {
						continue
					}
					t.Errorf("non-llm/vlm model %s/%s has max_input_tokens", supplier.Name, model.Name)
					continue
				}
				if model.MaxInputTokens == nil {
					t.Errorf("%s model %s/%s is missing max_input_tokens", model.Type, supplier.Name, model.Name)
					continue
				}
				limitCount++
				if !maxInputTokensPattern.MatchString(*model.MaxInputTokens) {
					t.Errorf("%s model %s/%s has invalid max_input_tokens", model.Type, supplier.Name, model.Name)
				}
			}
		}
	}
	if limitCount == 0 {
		t.Fatal("model catalog contains no llm or vlm max_input_tokens entries")
	}
}

func TestModelCatalogQwenEmbeddingModelsMatchBailian(t *testing.T) {
	yamlBytes, err := os.ReadFile(filepath.Join("..", "config", "model_catalog.yaml"))
	if err != nil {
		t.Fatalf("read model catalog: %v", err)
	}
	catalog, err := loadModelCatalog(yamlBytes)
	if err != nil {
		t.Fatalf("parse model catalog: %v", err)
	}

	want := map[string]struct{}{
		"text-embedding-async-v1": {},
		"text-embedding-async-v2": {},
		"text-embedding-v1":       {},
		"text-embedding-v2":       {},
		"text-embedding-v3":       {},
		"text-embedding-v4":       {},
	}
	got := map[string]struct{}{}
	for _, section := range catalog {
		for _, supplier := range section.Suppliers {
			if supplier.Name != "Qwen" {
				continue
			}
			for _, model := range supplier.Models {
				if model.Type == "embed" {
					got[model.Name] = struct{}{}
				}
			}
		}
	}
	if len(got) != len(want) {
		t.Fatalf("Qwen embedding models = %v, want %v", got, want)
	}
	for name := range want {
		if _, ok := got[name]; !ok {
			t.Errorf("Qwen embedding model %q is missing", name)
		}
	}
}

func TestUpsertDefaultModelPersistsAndBackfillsMaxInputTokens(t *testing.T) {
	dbName := "seed_model_" + time.Now().Format("150405.000000000")
	db, err := gorm.Open(sqlite.Open("file:"+dbName+"?mode=memory&cache=shared"), &gorm.Config{})
	if err != nil {
		t.Fatalf("open sqlite: %v", err)
	}
	if err := db.AutoMigrate(
		&orm.DefaultModelProvider{},
		&orm.DefaultModel{},
		&orm.UserModelProvider{},
		&orm.UserModelProviderGroupModel{},
	); err != nil {
		t.Fatalf("migrate: %v", err)
	}

	now := time.Now().UTC()
	if err := db.Create(&orm.DefaultModelProvider{
		ID: "default-qwen", Name: "Qwen", Description: "Qwen", CreatedAt: now, UpdatedAt: now,
	}).Error; err != nil {
		t.Fatalf("create default provider: %v", err)
	}
	if err := db.Create(&orm.UserModelProvider{
		ID:                     "user-provider-qwen",
		DefaultModelProviderID: "default-qwen",
		Name:                   "Qwen",
		Capabilities:           "has_models",
		BaseModel: orm.BaseModel{
			CreateUserID: "user-1", CreatedAt: now, UpdatedAt: now,
		},
	}).Error; err != nil {
		t.Fatalf("create user provider: %v", err)
	}
	if err := db.Create(&orm.UserModelProviderGroupModel{
		ID:                       "user-model-qwen",
		UserModelProviderID:      "user-provider-qwen",
		UserModelProviderGroupID: "group-qwen",
		ProviderName:             "Qwen",
		Name:                     "qwen2.5-7b-instruct-1m",
		ModelType:                "llm",
		IsDefault:                true,
		BaseModel: orm.BaseModel{
			CreateUserID: "user-1", CreatedAt: now, UpdatedAt: now,
		},
	}).Error; err != nil {
		t.Fatalf("create user model: %v", err)
	}

	limit := "1M"
	if err := upsertDefaultModel(db, now.Add(time.Minute), "default-qwen", "Qwen", catalogModel{
		Name: "qwen2.5-7b-instruct-1m", Type: "llm", MaxInputTokens: &limit,
	}); err != nil {
		t.Fatalf("upsert default model: %v", err)
	}

	var defaultModel orm.DefaultModel
	if err := db.Where("default_model_provider_id = ? AND name = ?", "default-qwen", "qwen2.5-7b-instruct-1m").Take(&defaultModel).Error; err != nil {
		t.Fatalf("query default model: %v", err)
	}
	if defaultModel.MaxInputTokens == nil || *defaultModel.MaxInputTokens != limit {
		t.Fatalf("default max_input_tokens = %v, want %s", defaultModel.MaxInputTokens, limit)
	}

	var userModel orm.UserModelProviderGroupModel
	if err := db.Where("id = ?", "user-model-qwen").Take(&userModel).Error; err != nil {
		t.Fatalf("query user model: %v", err)
	}
	if userModel.MaxInputTokens == nil || *userModel.MaxInputTokens != limit {
		t.Fatalf("user max_input_tokens = %v, want %s", userModel.MaxInputTokens, limit)
	}
}

func TestUpsertDefaultModelClearsRemovedMaxInputTokens(t *testing.T) {
	dbName := "seed_clear_model_" + time.Now().Format("150405.000000000")
	db, err := gorm.Open(sqlite.Open("file:"+dbName+"?mode=memory&cache=shared"), &gorm.Config{})
	if err != nil {
		t.Fatalf("open sqlite: %v", err)
	}
	if err := db.AutoMigrate(
		&orm.DefaultModelProvider{},
		&orm.DefaultModel{},
		&orm.UserModelProvider{},
		&orm.UserModelProviderGroupModel{},
	); err != nil {
		t.Fatalf("migrate: %v", err)
	}

	now := time.Now().UTC()
	if err := db.Create(&orm.DefaultModelProvider{
		ID: "default-qwen", Name: "Qwen", Description: "Qwen", CreatedAt: now, UpdatedAt: now,
	}).Error; err != nil {
		t.Fatalf("create default provider: %v", err)
	}
	if err := db.Create(&orm.UserModelProvider{
		ID:                     "user-provider-qwen",
		DefaultModelProviderID: "default-qwen",
		Name:                   "Qwen",
		Capabilities:           "has_models",
		BaseModel: orm.BaseModel{
			CreateUserID: "user-1", CreatedAt: now, UpdatedAt: now,
		},
	}).Error; err != nil {
		t.Fatalf("create user provider: %v", err)
	}

	limit := "1M"
	if err := db.Create(&orm.DefaultModel{
		ID:                     "default-model-qwen",
		DefaultModelProviderID: "default-qwen",
		ProviderName:           "Qwen",
		Name:                   "qwen2.5-7b-instruct-1m",
		ModelType:              "llm",
		MaxInputTokens:         &limit,
		CreatedAt:              now,
		UpdatedAt:              now,
	}).Error; err != nil {
		t.Fatalf("create default model: %v", err)
	}
	if err := db.Create(&orm.UserModelProviderGroupModel{
		ID:                       "user-model-qwen",
		UserModelProviderID:      "user-provider-qwen",
		UserModelProviderGroupID: "group-qwen",
		ProviderName:             "Qwen",
		Name:                     "qwen2.5-7b-instruct-1m",
		ModelType:                "llm",
		MaxInputTokens:           &limit,
		IsDefault:                true,
		BaseModel: orm.BaseModel{
			CreateUserID: "user-1", CreatedAt: now, UpdatedAt: now,
		},
	}).Error; err != nil {
		t.Fatalf("create user model: %v", err)
	}

	if err := upsertDefaultModel(db, now.Add(time.Minute), "default-qwen", "Qwen", catalogModel{
		Name: "qwen2.5-7b-instruct-1m", Type: "llm",
	}); err != nil {
		t.Fatalf("upsert default model: %v", err)
	}

	var defaultModel orm.DefaultModel
	if err := db.Where("id = ?", "default-model-qwen").Take(&defaultModel).Error; err != nil {
		t.Fatalf("query default model: %v", err)
	}
	if defaultModel.MaxInputTokens != nil {
		t.Fatalf("default max_input_tokens = %s, want null", *defaultModel.MaxInputTokens)
	}

	var userModel orm.UserModelProviderGroupModel
	if err := db.Where("id = ?", "user-model-qwen").Take(&userModel).Error; err != nil {
		t.Fatalf("query user model: %v", err)
	}
	if userModel.MaxInputTokens != nil {
		t.Fatalf("user max_input_tokens = %s, want null", *userModel.MaxInputTokens)
	}
}

func TestUpsertDefaultModelRejectsInvalidMaxInputTokens(t *testing.T) {
	zero := "0"
	err := upsertDefaultModel(&gorm.DB{}, time.Now(), "provider", "Provider", catalogModel{
		Name: "llm-model", Type: "llm", MaxInputTokens: &zero,
	})
	if err == nil || err.Error() != "model max_input_tokens must use a positive K or M value, for example 128K or 1M" {
		t.Fatalf("error = %v, want max_input_tokens validation error", err)
	}
}

func TestUpsertDefaultModelRejectsMaxInputTokensForNonLLMOrVLM(t *testing.T) {
	limit := "8K"
	err := upsertDefaultModel(&gorm.DB{}, time.Now(), "provider", "Provider", catalogModel{
		Name: "embedding-model", Type: "embed", MaxInputTokens: &limit,
	})
	if err == nil || err.Error() != "model max_input_tokens is only supported for llm or vlm models" {
		t.Fatalf("error = %v, want max_input_tokens llm/vlm-only validation error", err)
	}
}

func TestSyncRemovedDefaultModelsPreservesCustomModels(t *testing.T) {
	dbName := "sync_removed_models_" + time.Now().Format("150405.000000000")
	db, err := gorm.Open(sqlite.Open("file:"+dbName+"?mode=memory&cache=shared"), &gorm.Config{})
	if err != nil {
		t.Fatalf("open sqlite: %v", err)
	}
	if err := db.AutoMigrate(
		&orm.DefaultModel{},
		&orm.UserModelProvider{},
		&orm.UserModelProviderGroupModel{},
		&orm.UserSelectedModel{},
	); err != nil {
		t.Fatalf("migrate: %v", err)
	}

	now := time.Now().UTC()
	models := []orm.DefaultModel{
		{ID: "kept-default", DefaultModelProviderID: "default-qwen", ProviderName: "Qwen", Name: "text-embedding-v4", ModelType: "embed", CreatedAt: now, UpdatedAt: now},
		{ID: "removed-default", DefaultModelProviderID: "default-qwen", ProviderName: "Qwen", Name: "qwen3-embedding-32m", ModelType: "embed", CreatedAt: now, UpdatedAt: now},
	}
	if err := db.Create(&models).Error; err != nil {
		t.Fatalf("create default models: %v", err)
	}
	if err := db.Create(&orm.UserModelProvider{
		ID: "user-provider-qwen", DefaultModelProviderID: "default-qwen", Name: "Qwen",
		BaseModel: orm.BaseModel{CreateUserID: "user-1", CreatedAt: now, UpdatedAt: now},
	}).Error; err != nil {
		t.Fatalf("create user provider: %v", err)
	}
	userModels := []orm.UserModelProviderGroupModel{
		{ID: "removed-user-default", UserModelProviderID: "user-provider-qwen", UserModelProviderGroupID: "group-qwen", ProviderName: "Qwen", Name: "qwen3-embedding-32m", ModelType: "embed", IsDefault: true, BaseModel: orm.BaseModel{CreateUserID: "user-1", CreatedAt: now, UpdatedAt: now}},
		{ID: "custom-user-model", UserModelProviderID: "user-provider-qwen", UserModelProviderGroupID: "group-qwen", ProviderName: "Qwen", Name: "custom-embedding", ModelType: "embed", IsDefault: false, BaseModel: orm.BaseModel{CreateUserID: "user-1", CreatedAt: now, UpdatedAt: now}},
	}
	if err := db.Create(&userModels).Error; err != nil {
		t.Fatalf("create user models: %v", err)
	}
	if err := db.Create(&orm.UserSelectedModel{
		UserID: "user-1", ModelKey: "embed_main", UserModelProviderGroupModelID: "removed-user-default", CreatedAt: now, UpdatedAt: now,
	}).Error; err != nil {
		t.Fatalf("create selected model: %v", err)
	}

	later := now.Add(time.Minute)
	if err := syncRemovedDefaultModels(db, later, "default-qwen", []catalogModel{{Name: "text-embedding-v4", Type: "embed"}}); err != nil {
		t.Fatalf("sync removed models: %v", err)
	}

	var removedDefault orm.DefaultModel
	if err := db.Unscoped().Where("id = ?", "removed-default").Take(&removedDefault).Error; err != nil {
		t.Fatalf("query removed default model: %v", err)
	}
	if removedDefault.DeletedAt == nil {
		t.Fatal("removed default model was not soft-deleted")
	}
	var removedUserDefault orm.UserModelProviderGroupModel
	if err := db.Unscoped().Where("id = ?", "removed-user-default").Take(&removedUserDefault).Error; err != nil {
		t.Fatalf("query removed user default model: %v", err)
	}
	if removedUserDefault.DeletedAt == nil {
		t.Fatal("removed user default model was not soft-deleted")
	}
	var customCount int64
	if err := db.Model(&orm.UserModelProviderGroupModel{}).Where("id = ?", "custom-user-model").Count(&customCount).Error; err != nil {
		t.Fatalf("query custom model: %v", err)
	}
	if customCount != 1 {
		t.Fatalf("active custom model count = %d, want 1", customCount)
	}
	var selectionCount int64
	if err := db.Model(&orm.UserSelectedModel{}).Where("user_id = ?", "user-1").Count(&selectionCount).Error; err != nil {
		t.Fatalf("query selected models: %v", err)
	}
	if selectionCount != 0 {
		t.Fatalf("selected model count = %d, want 0", selectionCount)
	}
}
