package quoting

import (
	"encoding/json"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"time"

	"hyperliquid-market-maker/internal/config"
)

// EvalSnapshot is a compact record of an optimizer evaluation, retained in
// a ring buffer for inclusion in post-mortem dumps.
type EvalSnapshot struct {
	Timestamp time.Time          `json:"timestamp"`
	Coin      string             `json:"coin"`
	State     map[string]float64 `json:"state"` // 4D state bucket values
	Knobs     map[string]float64 `json:"knobs"` // current knob values
	Fired     []string           `json:"fired,omitempty"`
	Emergency bool               `json:"emergency"`
	EvalCount int                `json:"eval_count"`
}

// RefreshAttribution is a compact record of a single quote refresh decision.
type RefreshAttribution struct {
	Timestamp time.Time `json:"timestamp"`
	Coin      string    `json:"coin"`
	Trigger   string    `json:"trigger"` // e.g. "scheduled", "post_fill", "opt_delta", "force"
	Action    string    `json:"action"`  // "refresh" / "skip"
	Orders    int       `json:"orders"`
}

// FillRecord is a compact record of a single fill, retained for the entire
// session and dumped at halt time.
type FillRecord struct {
	Timestamp    time.Time `json:"timestamp"`
	Coin         string    `json:"coin"`
	Side         string    `json:"side"`
	Size         float64   `json:"size"`
	Price        float64   `json:"price"`
	MidAtFill    float64   `json:"mid_at_fill"`
	Notional     float64   `json:"notional"`
	Fee          float64   `json:"fee"`
	RealizedPnL  float64   `json:"realized_pnl"`
	NetPnL       float64   `json:"net_pnl"`
	WallFronted  bool      `json:"wall_fronted"`
	Markout5sBps float64   `json:"markout_5s_bps,omitempty"`
}

// ringBuffer is a simple generic fixed-capacity circular buffer.
type ringBuffer[T any] struct {
	items []T
	head  int
	count int
	cap   int
}

func newRingBuffer[T any](capacity int) *ringBuffer[T] {
	if capacity < 1 {
		capacity = 1
	}
	return &ringBuffer[T]{
		items: make([]T, capacity),
		cap:   capacity,
	}
}

func (rb *ringBuffer[T]) Add(item T) {
	rb.items[rb.head] = item
	rb.head = (rb.head + 1) % rb.cap
	if rb.count < rb.cap {
		rb.count++
	}
}

// ItemsOldestFirst returns the buffered items in insertion order.
func (rb *ringBuffer[T]) ItemsOldestFirst() []T {
	out := make([]T, rb.count)
	start := 0
	if rb.count == rb.cap {
		start = rb.head
	}
	for i := 0; i < rb.count; i++ {
		idx := (start + i) % rb.cap
		out[i] = rb.items[idx]
	}
	return out
}

// PostMortemDumper buffers recent optimizer evals, refresh attributions, and
// the entire session's fills. On Dump it writes a structured JSON file to disk
// and rotates older files. All methods are nil-receiver-safe and concurrent-safe.
type PostMortemDumper struct {
	cfg            config.PostMortemConfig
	mu             sync.Mutex
	history        *ringBuffer[EvalSnapshot]
	refreshHistory *ringBuffer[RefreshAttribution]
	fills          []FillRecord
}

// NewPostMortemDumper constructs a dumper. When cfg.Enabled is false, the
// returned dumper is still non-nil but every public method is a no-op.
// Defaults are applied for empty/zero fields when enabled.
func NewPostMortemDumper(cfg config.PostMortemConfig) *PostMortemDumper {
	if cfg.Enabled {
		if cfg.OutputDir == "" {
			cfg.OutputDir = "postmortem/"
		}
		if cfg.FilenameFormat == "" {
			cfg.FilenameFormat = "{coin}-{YYYYMMDD}-{HHMMSS}-{reason}.json"
		}
		if cfg.IncludeLastNEvals == 0 {
			cfg.IncludeLastNEvals = 20
		}
		if cfg.IncludeLastNRefreshes == 0 {
			cfg.IncludeLastNRefreshes = 20
		}
		if cfg.RotateKeepCount == 0 {
			cfg.RotateKeepCount = 200
		}
	}
	d := &PostMortemDumper{cfg: cfg}
	if cfg.Enabled {
		d.history = newRingBuffer[EvalSnapshot](cfg.IncludeLastNEvals)
		d.refreshHistory = newRingBuffer[RefreshAttribution](cfg.IncludeLastNRefreshes)
	}
	return d
}

// Enabled reports whether the dumper will record/dump anything.
func (d *PostMortemDumper) Enabled() bool {
	if d == nil {
		return false
	}
	return d.cfg.Enabled
}

// Config returns the resolved (defaults-applied) config.
func (d *PostMortemDumper) Config() config.PostMortemConfig {
	if d == nil {
		return config.PostMortemConfig{}
	}
	return d.cfg
}

// RecordEval appends an eval snapshot to the ring buffer.
func (d *PostMortemDumper) RecordEval(snap EvalSnapshot) {
	if d == nil || !d.cfg.Enabled {
		return
	}
	d.mu.Lock()
	d.history.Add(snap)
	d.mu.Unlock()
}

// RecordRefresh appends a refresh attribution to the ring buffer.
func (d *PostMortemDumper) RecordRefresh(attr RefreshAttribution) {
	if d == nil || !d.cfg.Enabled {
		return
	}
	d.mu.Lock()
	d.refreshHistory.Add(attr)
	d.mu.Unlock()
}

// RecordFill appends a fill to the session-long fills slice.
func (d *PostMortemDumper) RecordFill(f FillRecord) {
	if d == nil || !d.cfg.Enabled {
		return
	}
	d.mu.Lock()
	d.fills = append(d.fills, f)
	d.mu.Unlock()
}

// Dump writes a JSON snapshot of the buffered state plus the supplied
// finalState map. Returns the written file path. No-op (returns "", nil) when
// the dumper is disabled or nil. Per-side stats are computed from the buffered
// fills filtered by coin (or all fills when coin == "all"/"" or unknown).
func (d *PostMortemDumper) Dump(coin, reason string, finalState map[string]any) (string, error) {
	if d == nil || !d.cfg.Enabled {
		return "", nil
	}

	now := time.Now()

	// Snapshot under lock, then release before doing JSON marshal + I/O so
	// concurrent RecordFill/RecordEval/RecordRefresh aren't blocked on a
	// potentially slow filesystem write.
	d.mu.Lock()
	evals := d.history.ItemsOldestFirst()
	refreshes := d.refreshHistory.ItemsOldestFirst()
	var fills []FillRecord
	if d.cfg.IncludeAllFills {
		if coin == "" || coin == "all" {
			fills = make([]FillRecord, len(d.fills))
			copy(fills, d.fills)
		} else {
			fills = make([]FillRecord, 0, len(d.fills))
			for _, f := range d.fills {
				if f.Coin == coin {
					fills = append(fills, f)
				}
			}
		}
	}
	d.mu.Unlock()
	perSide := perSideStats(fills)

	startEquity, _ := finalState["start_equity"].(float64)
	endEquity, _ := finalState["end_equity"].(float64)
	durationSec, _ := finalState["duration_sec"].(float64)

	var sessionPnLBps float64
	if startEquity > 0 {
		sessionPnLBps = (endEquity - startEquity) / startEquity * 10000.0
	}

	payload := map[string]any{
		"coin":      coin,
		"reason":    reason,
		"timestamp": now.Format(time.RFC3339Nano),
		"session": map[string]any{
			"start_equity":    startEquity,
			"end_equity":      endEquity,
			"session_pnl_bps": sessionPnLBps,
			"duration_sec":    durationSec,
		},
		"final_state":    finalState,
		"last_evals":     evals,
		"last_refreshes": refreshes,
		"fills":          fills,
		"per_side_stats": perSide,
	}

	data, err := json.MarshalIndent(payload, "", "  ")
	if err != nil {
		return "", fmt.Errorf("postmortem: marshal: %w", err)
	}

	if err := os.MkdirAll(d.cfg.OutputDir, 0o755); err != nil {
		return "", fmt.Errorf("postmortem: mkdir %q: %w", d.cfg.OutputDir, err)
	}

	filename := formatFilename(d.cfg.FilenameFormat, coin, reason, now)
	path := filepath.Join(d.cfg.OutputDir, filename)
	if err := os.WriteFile(path, data, 0o644); err != nil {
		return "", fmt.Errorf("postmortem: write %q: %w", path, err)
	}

	if err := rotate(d.cfg.OutputDir, d.cfg.RotateKeepCount); err != nil {
		// Don't fail the dump on rotation errors; just log.
		slog.Warn("post-mortem rotation failed", "dir", d.cfg.OutputDir, "error", err)
	}

	slog.Info("post-mortem written",
		"path", path,
		"reason", reason,
		"coin", coin,
		"size_bytes", len(data),
	)
	return path, nil
}

// formatFilename substitutes the supported placeholders. Sanitizes coin and
// reason so colon/slash-bearing values don't escape the output directory.
func formatFilename(format, coin, reason string, t time.Time) string {
	if format == "" {
		format = "{coin}-{YYYYMMDD}-{HHMMSS}-{reason}.json"
	}
	out := format
	out = strings.ReplaceAll(out, "{coin}", sanitizePathComponent(coin))
	out = strings.ReplaceAll(out, "{reason}", sanitizePathComponent(reason))
	out = strings.ReplaceAll(out, "{YYYYMMDD}", t.Format("20060102"))
	out = strings.ReplaceAll(out, "{HHMMSS}", t.Format("150405"))
	return out
}

func sanitizePathComponent(s string) string {
	if s == "" {
		return "unknown"
	}
	r := strings.NewReplacer(
		"/", "_",
		"\\", "_",
		":", "_",
		" ", "_",
		"..", "_",
	)
	return r.Replace(s)
}

// rotate enforces the keep-count by mtime over the dir's *.json files.
func rotate(dir string, keep int) error {
	if keep <= 0 {
		return nil
	}
	entries, err := os.ReadDir(dir)
	if err != nil {
		return err
	}
	type fileInfo struct {
		path  string
		mtime time.Time
	}
	var files []fileInfo
	for _, e := range entries {
		if e.IsDir() || !strings.HasSuffix(e.Name(), ".json") {
			continue
		}
		info, err := e.Info()
		if err != nil {
			continue
		}
		files = append(files, fileInfo{path: filepath.Join(dir, e.Name()), mtime: info.ModTime()})
	}
	if len(files) <= keep {
		return nil
	}
	sort.Slice(files, func(i, j int) bool {
		return files[i].mtime.Before(files[j].mtime) // oldest first
	})
	toDelete := files[:len(files)-keep]
	for _, f := range toDelete {
		if err := os.Remove(f.path); err != nil {
			return err
		}
	}
	return nil
}

// perSideStats summarizes fills by side for the post-mortem payload.
func perSideStats(fills []FillRecord) map[string]map[string]float64 {
	out := map[string]map[string]float64{
		"buy":  {"count": 0, "notional": 0, "fees": 0, "realized_pnl": 0, "net_pnl": 0},
		"sell": {"count": 0, "notional": 0, "fees": 0, "realized_pnl": 0, "net_pnl": 0},
	}
	for _, f := range fills {
		side := f.Side
		if side != "buy" && side != "sell" {
			continue
		}
		out[side]["count"]++
		out[side]["notional"] += f.Notional
		out[side]["fees"] += f.Fee
		out[side]["realized_pnl"] += f.RealizedPnL
		out[side]["net_pnl"] += f.NetPnL
	}
	return out
}
