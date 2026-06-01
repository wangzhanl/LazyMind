package schedule

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"strconv"
	"strings"
	"time"

	"github.com/lazymind/scan_control_plane/internal/sourceengine/connector"
	store "github.com/lazymind/scan_control_plane/internal/store/source"
)

const (
	SyncModeManual    = "manual"
	SyncModeScheduled = "scheduled"
	SyncModeWatch     = "watch"

	TriggerTypeManual    = "manual"
	TriggerTypeScheduled = "scheduled"
	TriggerTypeWatch     = "watch"
	TriggerTypeReconcile = "reconcile"

	watchReconcileInterval = 10 * time.Minute
)

type Store interface {
	GetBinding(ctx context.Context, sourceID, bindingID string) (store.Binding, error)
	GetSyncRun(ctx context.Context, runID string) (store.SyncRun, error)
	GetSyncCheckpoint(ctx context.Context, bindingID string) (store.SyncCheckpoint, error)
	ListDueSyncCheckpoints(ctx context.Context, now time.Time, limit int) ([]store.SyncCheckpoint, error)
	FinishSyncRun(ctx context.Context, runID, workerID string, finish store.SyncRunFinish) (store.SyncRun, bool, error)
}

type SyncRunQueue interface {
	EnqueueSyncRun(ctx context.Context, run store.SyncRun) (store.SyncRun, bool, error)
}

type CheckpointScheduleEngine struct {
	store        Store
	queue        SyncRunQueue
	tasks        PendingTaskPlanner
	clock        func() time.Time
	newID        func(prefix string) string
	retryBackoff func(retryCount int64) time.Duration
}

type Option func(*CheckpointScheduleEngine)

type PendingTaskPlanner interface {
	GeneratePendingTasks(ctx context.Context, sourceID, bindingID, runID string) error
}

func NewCheckpointScheduleEngine(store Store, queue SyncRunQueue, options ...Option) *CheckpointScheduleEngine {
	if store == nil {
		panic("schedule store is required")
	}
	if queue == nil {
		panic("schedule queue is required")
	}
	e := &CheckpointScheduleEngine{
		store: store,
		queue: queue,
		clock: time.Now,
		newID: func(prefix string) string {
			return prefix + "-" + time.Now().UTC().Format("20060102150405.000000000")
		},
		retryBackoff: func(retryCount int64) time.Duration {
			if retryCount <= 0 {
				retryCount = 1
			}
			delay := time.Duration(retryCount) * time.Minute
			if delay > time.Hour {
				return time.Hour
			}
			return delay
		},
	}
	for _, option := range options {
		option(e)
	}
	return e
}

func WithClock(clock func() time.Time) Option {
	return func(e *CheckpointScheduleEngine) {
		if clock != nil {
			e.clock = clock
		}
	}
}

func WithIDGenerator(newID func(prefix string) string) Option {
	return func(e *CheckpointScheduleEngine) {
		if newID != nil {
			e.newID = newID
		}
	}
}

func WithRetryBackoff(backoff func(retryCount int64) time.Duration) Option {
	return func(e *CheckpointScheduleEngine) {
		if backoff != nil {
			e.retryBackoff = backoff
		}
	}
}

func WithTaskPlanner(planner PendingTaskPlanner) Option {
	return func(e *CheckpointScheduleEngine) {
		e.tasks = planner
	}
}

func (e *CheckpointScheduleEngine) BuildCheckpoint(_ context.Context, binding store.Binding, now time.Time) (store.SyncCheckpoint, error) {
	next, err := nextSyncAt(binding, now)
	if err != nil {
		return store.SyncCheckpoint{}, err
	}
	return store.SyncCheckpoint{
		SourceID:          binding.SourceID,
		BindingID:         binding.BindingID,
		BindingGeneration: binding.BindingGeneration,
		NextSyncAt:        next,
		LastError:         store.JSON{},
		CreatedAt:         now,
		UpdatedAt:         now,
	}, nil
}

func (e *CheckpointScheduleEngine) TriggerInitialSync(ctx context.Context, binding store.Binding) ([]string, error) {
	if binding.SyncMode == SyncModeScheduled || binding.SyncMode == SyncModeWatch {
		return nil, nil
	}
	intent, err := e.enqueueBindingRun(ctx, binding, TriggerTypeManual, connector.ScopeTypeFull, nil, "", e.clock().UTC())
	if err != nil || intent.Run.RunID == "" {
		return nil, err
	}
	return []string{intent.Run.RunID}, nil
}

type ManualSyncRequest struct {
	RequestID string
	SourceID  string
	BindingID string
	ScopeType connector.ScopeType
	ScopeRef  connector.ScopeRef
}

type SyncRunIntent struct {
	Run     store.SyncRun
	Created bool
}

func (e *CheckpointScheduleEngine) EnqueueManualSync(ctx context.Context, req ManualSyncRequest) (SyncRunIntent, error) {
	binding, err := e.store.GetBinding(ctx, req.SourceID, req.BindingID)
	if err != nil {
		return SyncRunIntent{}, err
	}
	scopeType := req.ScopeType
	if scopeType == "" {
		scopeType = connector.ScopeTypeFull
	}
	return e.enqueueBindingRun(ctx, binding, TriggerTypeManual, scopeType, req.ScopeRef, req.RequestID, e.clock().UTC())
}

func (e *CheckpointScheduleEngine) EnqueueDueSyncRuns(ctx context.Context, limit int) ([]SyncRunIntent, error) {
	now := e.clock().UTC()
	checkpoints, err := e.store.ListDueSyncCheckpoints(ctx, now, limit)
	if err != nil {
		return nil, err
	}
	intents := make([]SyncRunIntent, 0, len(checkpoints))
	for _, checkpoint := range checkpoints {
		binding, err := e.store.GetBinding(ctx, checkpoint.SourceID, checkpoint.BindingID)
		if err != nil {
			return intents, err
		}
		trigger := TriggerTypeScheduled
		if binding.SyncMode == SyncModeWatch {
			trigger = TriggerTypeReconcile
		}
		intent, err := e.enqueueBindingRun(ctx, binding, trigger, connector.ScopeTypeFull, nil, "", now)
		if err != nil {
			return intents, err
		}
		intents = append(intents, intent)
	}
	return intents, nil
}

type FinishRunRequest struct {
	RunID          string
	WorkerID       string
	Status         string
	Cursor         string
	Coverage       store.JSON
	SeenCount      int64
	NewCount       int64
	ModifiedCount  int64
	DeletedCount   int64
	UnchangedCount int64
	ErrorCode      string
	ErrorMessage   string
	NextSyncAt     *time.Time
}

func (e *CheckpointScheduleEngine) FinishRun(ctx context.Context, req FinishRunRequest) (store.SyncRun, bool, error) {
	run, err := e.store.GetSyncRun(ctx, req.RunID)
	if err != nil {
		return store.SyncRun{}, false, err
	}
	binding, err := e.store.GetBinding(ctx, run.SourceID, run.BindingID)
	if err != nil {
		return store.SyncRun{}, false, err
	}
	checkpoint, err := e.store.GetSyncCheckpoint(ctx, run.BindingID)
	if err != nil {
		return store.SyncRun{}, false, err
	}
	finish, err := e.buildFinish(binding, checkpoint, req)
	if err != nil {
		return store.SyncRun{}, false, err
	}
	finished, ok, err := e.store.FinishSyncRun(ctx, req.RunID, req.WorkerID, finish)
	if err != nil || !ok || finished.Status != store.SyncRunStatusSucceeded || e.tasks == nil {
		return finished, ok, err
	}
	err = e.tasks.GeneratePendingTasks(ctx, finished.SourceID, finished.BindingID, finished.RunID)
	return finished, ok, err
}

func (e *CheckpointScheduleEngine) enqueueBindingRun(ctx context.Context, binding store.Binding, trigger string, scopeType connector.ScopeType, scopeRef connector.ScopeRef, requestID string, runAt time.Time) (SyncRunIntent, error) {
	run := store.SyncRun{
		RunID:             e.syncRunID(binding, requestID),
		SourceID:          binding.SourceID,
		BindingID:         binding.BindingID,
		BindingGeneration: binding.BindingGeneration,
		TriggerType:       trigger,
		ScopeType:         string(scopeType),
		ScopeRef:          scopeJSON(scopeRef),
		Coverage:          store.JSON{},
		Status:            store.SyncRunStatusPending,
		StartedAt:         runAt,
	}
	if requestID != "" {
		if persisted, err := e.store.GetSyncRun(ctx, run.RunID); err == nil {
			return SyncRunIntent{Run: persisted, Created: false}, nil
		} else if store.ErrorCodeOf(err) != store.ErrCodeNotFound {
			return SyncRunIntent{}, err
		}
	}
	persisted, created, err := e.queue.EnqueueSyncRun(ctx, run)
	return SyncRunIntent{Run: persisted, Created: created}, err
}

func (e *CheckpointScheduleEngine) syncRunID(binding store.Binding, requestID string) string {
	if requestID == "" {
		return e.newID("sync-run")
	}
	sum := sha256.Sum256([]byte(binding.SourceID + "\x00" + binding.BindingID + "\x00" + fmt.Sprint(binding.BindingGeneration) + "\x00" + requestID))
	return "sync-run-" + hex.EncodeToString(sum[:12])
}

func (e *CheckpointScheduleEngine) buildFinish(binding store.Binding, checkpoint store.SyncCheckpoint, req FinishRunRequest) (store.SyncRunFinish, error) {
	now := e.clock().UTC()
	status := req.Status
	if status == "" {
		status = store.SyncRunStatusSucceeded
	}
	next := req.NextSyncAt
	if status == store.SyncRunStatusSucceeded && next == nil {
		var err error
		next, err = nextSyncAt(binding, now)
		if err != nil {
			return store.SyncRunFinish{}, err
		}
	}
	if status != store.SyncRunStatusSucceeded && next == nil {
		retryAt := now.Add(e.retryBackoff(checkpoint.RetryCount + 1))
		next = &retryAt
	}
	return store.SyncRunFinish{
		Status:         status,
		Cursor:         req.Cursor,
		NextSyncAt:     next,
		Coverage:       req.Coverage,
		SeenCount:      req.SeenCount,
		NewCount:       req.NewCount,
		ModifiedCount:  req.ModifiedCount,
		DeletedCount:   req.DeletedCount,
		UnchangedCount: req.UnchangedCount,
		ErrorCode:      req.ErrorCode,
		ErrorMessage:   req.ErrorMessage,
		FinishedAt:     now,
	}, nil
}

func nextSyncAt(binding store.Binding, now time.Time) (*time.Time, error) {
	if binding.NextSyncAt != nil && binding.NextSyncAt.After(now) {
		return binding.NextSyncAt, nil
	}
	if binding.SyncMode == SyncModeWatch {
		next := now.Add(watchReconcileInterval)
		return &next, nil
	}
	if binding.SyncMode != SyncModeScheduled {
		return nil, nil
	}
	scheduleNow, err := scheduleNow(binding.ScheduleTZ, now)
	if err != nil {
		return nil, err
	}
	next, err := parseScheduleExpr(binding.ScheduleExpr, scheduleNow)
	if err != nil {
		return nil, err
	}
	return &next, nil
}

func scheduleNow(tz string, now time.Time) (time.Time, error) {
	tz = strings.TrimSpace(tz)
	if tz == "" {
		return now, nil
	}
	loc, err := time.LoadLocation(tz)
	if err != nil {
		return time.Time{}, fmt.Errorf("unsupported schedule timezone %q", tz)
	}
	return now.In(loc), nil
}

func parseScheduleExpr(expr string, now time.Time) (time.Time, error) {
	raw := strings.TrimSpace(expr)
	lower := strings.ToLower(raw)
	if strings.HasPrefix(lower, "daily@") {
		hour, minute, second, err := parseDailyTime(raw[len("daily@"):])
		if err != nil {
			return time.Time{}, err
		}
		next := time.Date(now.Year(), now.Month(), now.Day(), hour, minute, second, 0, now.Location())
		if !next.After(now) {
			next = next.Add(24 * time.Hour)
		}
		return next, nil
	}

	normalized := strings.TrimPrefix(lower, "@every ")
	normalized = strings.TrimPrefix(normalized, "every ")
	switch normalized {
	case "@hourly", "hourly":
		return now.Add(time.Hour), nil
	case "@daily", "daily":
		return now.Add(24 * time.Hour), nil
	}
	if duration, err := time.ParseDuration(normalized); err == nil && duration > 0 {
		return now.Add(duration), nil
	}
	return time.Time{}, fmt.Errorf("unsupported schedule expression %q", raw)
}

func ValidateScheduleExpr(expr string) error {
	_, err := parseScheduleExpr(expr, time.Now().UTC())
	return err
}

func ValidateSchedule(expr, tz string) error {
	now, err := scheduleNow(tz, time.Now().UTC())
	if err != nil {
		return err
	}
	_, err = parseScheduleExpr(expr, now)
	return err
}

func parseDailyTime(token string) (int, int, int, error) {
	parts := strings.Split(token, ":")
	if len(parts) != 2 && len(parts) != 3 {
		return 0, 0, 0, fmt.Errorf("unsupported schedule expression %q", "daily@"+token)
	}
	hour, err := parseFixedRange(parts[0], 0, 23)
	if err != nil {
		return 0, 0, 0, fmt.Errorf("unsupported schedule expression %q", "daily@"+token)
	}
	minute, err := parseFixedRange(parts[1], 0, 59)
	if err != nil {
		return 0, 0, 0, fmt.Errorf("unsupported schedule expression %q", "daily@"+token)
	}
	second := 0
	if len(parts) == 3 {
		second, err = parseFixedRange(parts[2], 0, 59)
		if err != nil {
			return 0, 0, 0, fmt.Errorf("unsupported schedule expression %q", "daily@"+token)
		}
	}
	return hour, minute, second, nil
}

func parseFixedRange(token string, min, max int) (int, error) {
	if len(token) != 2 {
		return 0, fmt.Errorf("invalid time token")
	}
	value, err := strconv.Atoi(token)
	if err != nil || value < min || value > max {
		return 0, fmt.Errorf("invalid time token")
	}
	return value, nil
}

func scopeJSON(scopeRef connector.ScopeRef) store.JSON {
	if len(scopeRef) == 0 {
		return store.JSON{}
	}
	out := make(store.JSON, len(scopeRef))
	for key, value := range scopeRef {
		out[key] = value
	}
	return out
}
