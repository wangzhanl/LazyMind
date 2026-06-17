package tree

import (
	"context"
	"encoding/json"
	"strings"
	"time"

	"github.com/redis/go-redis/v9"
)

const targetSearchCacheRedisPrefix = "scan:binding_target_search_cache:"

type redisTargetSearchCacheStore struct {
	client *redis.Client
}

type redisTargetSearchCachePayload struct {
	Nodes     []TreeNode `json:"nodes"`
	Complete  bool       `json:"complete"`
	Truncated bool       `json:"truncated"`
	LastError string     `json:"last_error,omitempty"`
}

func NewRedisTargetSearchCacheStore(rawURL string) (targetSearchCacheStore, error) {
	rawURL = strings.TrimSpace(rawURL)
	if rawURL == "" {
		return nil, nil
	}
	options, err := redis.ParseURL(rawURL)
	if err != nil {
		return nil, err
	}
	client := redis.NewClient(options)
	return &redisTargetSearchCacheStore{client: client}, nil
}

func (s *redisTargetSearchCacheStore) Get(ctx context.Context, key string) (targetSearchCacheSnapshot, bool, error) {
	if s == nil || s.client == nil {
		return targetSearchCacheSnapshot{}, false, nil
	}
	data, err := s.client.Get(ctx, s.dataKey(key)).Bytes()
	if err == redis.Nil {
		return targetSearchCacheSnapshot{}, false, nil
	}
	if err != nil {
		return targetSearchCacheSnapshot{}, false, err
	}
	var payload redisTargetSearchCachePayload
	if err := json.Unmarshal(data, &payload); err != nil {
		return targetSearchCacheSnapshot{}, false, err
	}
	return targetSearchCacheSnapshot{
		nodes:     append([]TreeNode(nil), payload.Nodes...),
		complete:  payload.Complete,
		truncated: payload.Truncated,
		lastError: payload.LastError,
	}, true, nil
}

func (s *redisTargetSearchCacheStore) Set(ctx context.Context, key string, snapshot targetSearchCacheSnapshot, ttl time.Duration) error {
	if s == nil || s.client == nil {
		return nil
	}
	payload := redisTargetSearchCachePayload{
		Nodes:     append([]TreeNode(nil), snapshot.nodes...),
		Complete:  snapshot.complete,
		Truncated: snapshot.truncated,
		LastError: snapshot.lastError,
	}
	data, err := json.Marshal(payload)
	if err != nil {
		return err
	}
	pipe := s.client.TxPipeline()
	pipe.Set(ctx, s.dataKey(key), data, ttl)
	pipe.Del(ctx, s.lockKey(key))
	_, err = pipe.Exec(ctx)
	return err
}

func (s *redisTargetSearchCacheStore) TryLock(ctx context.Context, key string, ttl time.Duration) (bool, error) {
	if s == nil || s.client == nil {
		return false, nil
	}
	lockTTL := ttl
	if lockTTL <= 0 {
		lockTTL = time.Minute
	}
	return s.client.SetNX(ctx, s.lockKey(key), "1", lockTTL).Result()
}

func (s *redisTargetSearchCacheStore) dataKey(key string) string {
	return targetSearchCacheRedisPrefix + targetSearchCacheRedisKey(key) + ":data"
}

func (s *redisTargetSearchCacheStore) lockKey(key string) string {
	return targetSearchCacheRedisPrefix + targetSearchCacheRedisKey(key) + ":lock"
}
