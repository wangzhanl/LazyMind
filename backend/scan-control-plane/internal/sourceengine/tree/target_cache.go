package tree

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/lazymind/scan_control_plane/internal/sourceengine/connector"
)

const (
	targetSearchCacheTTL        = 10 * time.Minute
	targetSearchCacheMaxObjects = 5000
	targetSearchCachePageDelay  = 200 * time.Millisecond
)

type targetSearchCache struct {
	mu       sync.Mutex
	entries  map[string]*targetSearchCacheEntry
	store    targetSearchCacheStore
	ttl      time.Duration
	maxItems int
	delay    time.Duration
}

type targetSearchCacheEntry struct {
	nodes     []TreeNode
	building  bool
	complete  bool
	truncated bool
	lastError string
	expiresAt time.Time
}

type targetSearchCacheSnapshot struct {
	nodes     []TreeNode
	building  bool
	complete  bool
	truncated bool
	lastError string
}

type targetSearchCacheStore interface {
	Get(ctx context.Context, key string) (targetSearchCacheSnapshot, bool, error)
	Set(ctx context.Context, key string, snapshot targetSearchCacheSnapshot, ttl time.Duration) error
	TryLock(ctx context.Context, key string, ttl time.Duration) (bool, error)
}

func newTargetSearchCache() *targetSearchCache {
	return &targetSearchCache{
		entries:  map[string]*targetSearchCacheEntry{},
		ttl:      targetSearchCacheTTL,
		maxItems: targetSearchCacheMaxObjects,
		delay:    targetSearchCachePageDelay,
	}
}

func (c *targetSearchCache) snapshotOrStart(ctx context.Context, conn connector.SourceConnector, req TargetTreeSearchRequest, build func(context.Context, connector.SourceConnector, TargetTreeSearchRequest) ([]TreeNode, bool, error)) targetSearchCacheSnapshot {
	if c == nil {
		return targetSearchCacheSnapshot{}
	}
	key := targetSearchCacheKey(req)
	if c.store != nil {
		if snapshot, ok, err := c.store.Get(ctx, key); err == nil && ok {
			return snapshot
		}
		if locked, err := c.store.TryLock(ctx, key, c.ttl); err == nil {
			if locked {
				go c.build(key, conn, req, build)
			}
			return targetSearchCacheSnapshot{building: true}
		}
	}
	return c.memorySnapshotOrStart(key, conn, req, build)
}

func (c *targetSearchCache) memorySnapshotOrStart(key string, conn connector.SourceConnector, req TargetTreeSearchRequest, build func(context.Context, connector.SourceConnector, TargetTreeSearchRequest) ([]TreeNode, bool, error)) targetSearchCacheSnapshot {
	now := time.Now()
	c.mu.Lock()
	entry := c.entries[key]
	if entry == nil || now.After(entry.expiresAt) {
		entry = &targetSearchCacheEntry{building: true, expiresAt: now.Add(c.ttl)}
		c.entries[key] = entry
		go c.build(key, conn, req, build)
	}
	snapshot := entry.snapshot()
	c.mu.Unlock()
	return snapshot
}

func (c *targetSearchCache) build(key string, conn connector.SourceConnector, req TargetTreeSearchRequest, build func(context.Context, connector.SourceConnector, TargetTreeSearchRequest) ([]TreeNode, bool, error)) {
	ctx, cancel := context.WithTimeout(context.Background(), c.ttl)
	defer cancel()
	nodes, truncated, err := build(ctx, conn, req)
	c.mu.Lock()
	defer c.mu.Unlock()
	entry := c.entries[key]
	if entry == nil {
		entry = &targetSearchCacheEntry{}
		c.entries[key] = entry
	}
	entry.nodes = nodes
	entry.truncated = truncated
	entry.complete = err == nil
	entry.building = false
	entry.lastError = ""
	if err != nil {
		entry.lastError = err.Error()
	}
	entry.expiresAt = time.Now().Add(c.ttl)
	if c.store != nil {
		snapshot := entry.snapshot()
		go func() {
			ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
			defer cancel()
			_ = c.store.Set(ctx, key, snapshot, c.ttl)
		}()
	}
}

func (e *targetSearchCacheEntry) snapshot() targetSearchCacheSnapshot {
	if e == nil {
		return targetSearchCacheSnapshot{}
	}
	nodes := append([]TreeNode(nil), e.nodes...)
	return targetSearchCacheSnapshot{
		nodes:     nodes,
		building:  e.building,
		complete:  e.complete,
		truncated: e.truncated,
		lastError: e.lastError,
	}
}

func targetSearchCacheKey(req TargetTreeSearchRequest) string {
	parts := []string{
		string(req.ConnectorType),
		req.AgentID,
		req.AuthConnectionID,
		stableProviderOptions(req.ProviderOptions),
	}
	return strings.Join(parts, "\x00")
}

func targetSearchCacheRedisKey(key string) string {
	sum := sha256.Sum256([]byte(key))
	return hex.EncodeToString(sum[:])
}

func stableProviderOptions(options map[string]any) string {
	if len(options) == 0 {
		return ""
	}
	keys := make([]string, 0, len(options))
	for key := range options {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	out := make(map[string]any, len(options))
	for _, key := range keys {
		out[key] = options[key]
	}
	data, err := json.Marshal(out)
	if err != nil {
		return ""
	}
	return string(data)
}

func paginateCachedTargetNodes(nodes []TreeNode, keyword string, includeFiles bool, pageSize int, cursor string) (TreeNodePage, error) {
	offset, err := cursorOffset(cursor)
	if err != nil {
		return TreeNodePage{}, err
	}
	matches := make([]TreeNode, 0, pageSize)
	seen := 0
	for _, node := range nodes {
		if !includeFiles && !node.IsContainer {
			continue
		}
		if !treeNodeSearchMatches(node, keyword) {
			continue
		}
		seen++
		if seen <= offset {
			continue
		}
		matches = append(matches, node)
		if len(matches) > pageSize {
			return TreeNodePage{
				Items:      matches[:pageSize],
				HasMore:    true,
				NextCursor: strconv.Itoa(offset + pageSize),
				SearchMode: SearchModeCache,
			}, nil
		}
	}
	return TreeNodePage{Items: matches, ListComplete: true, SearchMode: SearchModeCache}, nil
}

func treeNodeSearchMatches(node TreeNode, keyword string) bool {
	needle := strings.ToLower(strings.TrimSpace(keyword))
	if needle == "" {
		return true
	}
	for _, value := range []string{node.SearchName, node.DisplayName} {
		if strings.Contains(strings.ToLower(value), needle) {
			return true
		}
	}
	return false
}
