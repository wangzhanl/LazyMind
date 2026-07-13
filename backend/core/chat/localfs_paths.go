package chat

import (
	"context"
	"encoding/json"
	"fmt"
	"gorm.io/gorm"
	"io"
	"lazymind/core/common"
	"lazymind/core/datasource"
	"net/http"
	"net/url"
	"os"
	"strconv"
	"strings"
	"time"
)

const (
	defaultScanTenantID       = ""
	localFSScanPageSize       = 200
	localFSScanMaxPages       = 100
	localFSScanRequestTimeout = 10 * time.Second
)

var localFSScanHTTPClient = &http.Client{Timeout: localFSScanRequestTimeout}
var allowedFileExtensions = map[string]bool{
	"pdf": true, "doc": true, "docx": true,
	"csv": true, "xls": true, "xlsx": true,
}

type scanSourceListResponse struct {
	Items []scanSourceListItem `json:"items"`
	Total int                  `json:"total"`
}
type scanSourceListItem struct {
	SourceID    string `json:"source_id"`
	Status      string `json:"status"`
	DatasetID   string `json:"dataset_id"`
	TenantID    string `json:"tenant_id,omitempty"`
	ChatEnabled bool   `json:"chat_enabled"`
}
type scanGetSourceResponse struct {
	Bindings []scanSourceBinding `json:"bindings"`
}
type scanSourceBinding struct {
	ConnectorType     string   `json:"connector_type"`
	TargetType        string   `json:"target_type"`
	TargetRef         string   `json:"target_ref"`
	Status            string   `json:"status"`
	DeletedAt         any      `json:"deleted_at"`
	IncludeExtensions []string `json:"include_extensions,omitempty"`
}
type scanSourceInfo struct {
	SourceID    string
	DatasetID   string
	TenantID    string
	ChatEnabled bool
}

func applyLocalFSPathsForChat(ctx context.Context, r *http.Request, db *gorm.DB, userID string, reqBody map[string]any) error {
	enabled, err := datasource.LoadLocalFSChatEnabled(ctx, db, userID)
	if err != nil {
		return err
	}
	if !enabled {
		fmt.Printf("[CORE_LOCALFS_DEBUG] chat_enabled=false user=%s\n", userID)
		return nil
	}
	fmt.Printf("[CORE_LOCALFS_DEBUG] chat_enabled=true user=%s\n", userID)
	sources, err := loadLocalFSSourcesForChat(ctx, r, userID)
	if err != nil {
		return err
	}
	reqBody["local_fs_sources"] = sources
	return nil
}
func loadLocalFSSourcesForChat(ctx context.Context, r *http.Request, userID string) ([]map[string]any, error) {
	sourceInfos, err := listReadableScanSourceInfos(ctx, r, userID)
	if err != nil {
		fmt.Printf("[CORE_LOCALFS_DEBUG] listReadableScanSourceInfos error: %v\n", err)
		return nil, err
	}
	fmt.Printf("[CORE_LOCALFS_DEBUG] sourceInfos=%+v\n", sourceInfos)
	var sources []map[string]any
	for _, info := range sourceInfos {
		if !info.ChatEnabled {
			continue
		}
		bindings, err := getScanSourceBindings(ctx, r, userID, info.SourceID)
		if err != nil {
			return nil, err
		}
		extensions := collectLocalFSExtensions(bindings)
		if len(extensions) == 0 {
			continue
		}
		tenantID := strings.TrimSpace(info.TenantID)
		if tenantID == "" {
			tenantID = "root"
		}
		path := fmt.Sprintf("%s/tenants/%s/datasets/%s/docs/files/",
			localFSUploadsRoot(),
			tenantID, info.DatasetID)
		sources = append(sources, map[string]any{
			"source_id":       info.SourceID,
			"paths":           []string{path},
			"file_extensions": extensions,
		})
	}
	fmt.Printf("[CORE_LOCALFS_DEBUG] local_fs_sources=%v\n", sources)
	return sources, nil
}
func listReadableScanSourceInfos(ctx context.Context, r *http.Request, userID string) ([]scanSourceInfo, error) {
	var infos []scanSourceInfo
	for page := 1; page <= localFSScanMaxPages; page++ {
		endpoint, err := scanControlPlaneURL("/api/scan/sources")
		if err != nil {
			return nil, err
		}
		query := endpoint.Query()
		query.Set("page", strconv.Itoa(page))
		query.Set("page_size", strconv.Itoa(localFSScanPageSize))
		query.Set("status", "ACTIVE")
		endpoint.RawQuery = query.Encode()
		var payload scanSourceListResponse
		if err := doScanControlPlaneJSON(ctx, r, userID, endpoint.String(), &payload); err != nil {
			return nil, err
		}
		for _, item := range payload.Items {
			sourceID := strings.TrimSpace(item.SourceID)
			if sourceID == "" || !isActiveStatus(item.Status) {
				continue
			}
			infos = append(infos, scanSourceInfo{
				SourceID:    sourceID,
				DatasetID:   strings.TrimSpace(item.DatasetID),
				TenantID:    strings.TrimSpace(item.TenantID),
				ChatEnabled: item.ChatEnabled,
			})
		}
		if len(payload.Items) == 0 || page*localFSScanPageSize >= payload.Total {
			break
		}
	}
	return infos, nil
}
func getScanSourceBindings(ctx context.Context, r *http.Request, userID, sourceID string) ([]scanSourceBinding, error) {
	endpoint, err := scanControlPlaneURL("/api/scan/sources/" + url.PathEscape(sourceID))
	if err != nil {
		return nil, err
	}
	query := endpoint.Query()
	query.Set("include_bindings", "true")
	query.Set("include_summary", "false")
	endpoint.RawQuery = query.Encode()
	var payload scanGetSourceResponse
	if err := doScanControlPlaneJSON(ctx, r, userID, endpoint.String(), &payload); err != nil {
		fmt.Printf("[CORE_LOCALFS_DEBUG] getScanSourceBindings error for source=%s: %v\n", sourceID, err)
		return nil, err
	}
	fmt.Printf("[CORE_LOCALFS_DEBUG] source=%s bindings=%+v\n", sourceID, payload.Bindings)
	return payload.Bindings, nil
}
func scanControlPlaneURL(path string) (*url.URL, error) {
	base := strings.TrimRight(common.ScanControlPlaneEndpoint(), "/")
	endpoint, err := url.Parse(base + path)
	if err != nil {
		return nil, err
	}
	return endpoint, nil
}
func doScanControlPlaneJSON(ctx context.Context, original *http.Request, userID, endpoint string, out any) error {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, endpoint, nil)
	if err != nil {
		return err
	}
	req.Header.Set("Accept", "application/json")
	req.Header.Set("X-User-ID", strings.TrimSpace(userID))
	req.Header.Set("X-Tenant-ID", scanTenantIDFromRequest(original))
	req.Header.Set("X-User-Role", "admin")
	if auth := strings.TrimSpace(original.Header.Get("Authorization")); auth != "" {
		req.Header.Set("Authorization", auth)
	}
	resp, err := localFSScanHTTPClient.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode < http.StatusOK || resp.StatusCode >= http.StatusMultipleChoices {
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 1024))
		return fmt.Errorf("scan-control-plane request failed: status=%d body=%s", resp.StatusCode, strings.TrimSpace(string(body)))
	}
	return json.NewDecoder(resp.Body).Decode(out)
}
func scanTenantIDFromRequest(r *http.Request) string {
	if tenantID := strings.TrimSpace(r.Header.Get("X-Tenant-ID")); tenantID != "" {
		return tenantID
	}
	return defaultScanTenantID
}
func isLocalFSTypeAndActive(binding scanSourceBinding) bool {
	if binding.DeletedAt != nil || !isActiveStatus(binding.Status) {
		return false
	}
	return strings.EqualFold(strings.TrimSpace(binding.ConnectorType), "local_fs") ||
		strings.EqualFold(strings.TrimSpace(binding.TargetType), "local_path")
}
func collectLocalFSExtensions(bindings []scanSourceBinding) []string {
	seen := map[string]bool{}
	var exts []string
	for _, b := range bindings {
		if !isLocalFSTypeAndActive(b) {
			continue
		}
		for _, ext := range b.IncludeExtensions {
			e := normalizeFileExtension(ext)
			if e != "" && !seen[e] {
				seen[e] = true
				exts = append(exts, e)
			}
		}
	}
	return exts
}
func localFSUploadsRoot() string {
	if root := strings.TrimSpace(os.Getenv("LAZYMIND_UPLOAD_ROOT")); root != "" {
		return strings.TrimRight(root, "/")
	}
	return "/var/lib/lazymind/uploads"
}
func normalizeFileExtension(ext string) string {
	e := strings.TrimSpace(ext)
	e = strings.TrimPrefix(e, ".")
	e = strings.ToLower(e)
	if !allowedFileExtensions[e] {
		return ""
	}
	return e
}
func isActiveStatus(status string) bool {
	status = strings.ToUpper(strings.TrimSpace(status))
	return status == "" || status == "ACTIVE"
}
