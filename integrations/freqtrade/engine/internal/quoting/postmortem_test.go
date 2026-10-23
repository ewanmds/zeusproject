package quoting

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"hyperliquid-market-maker/internal/config"
)

func newTestPostMortemConfig(t *testing.T) config.PostMortemConfig {
	t.Helper()
	return config.PostMortemConfig{
		Enabled:               true,
		OutputDir:             t.TempDir(),
		FilenameFormat:        "{coin}-{YYYYMMDD}-{HHMMSS}-{reason}.json",
		IncludeLastNEvals:     5,
		IncludeLastNRefreshes: 5,
		IncludeAllFills:       true,
		RotateKeepCount:       3,
	}
}

func TestPostMortemDumper_WritesValidJSONWithRequiredFields(t *testing.T) {
	cfg := newTestPostMortemConfig(t)
	d := NewPostMortemDumper(cfg)

	d.RecordEval(EvalSnapshot{
		Timestamp: time.Now(),
		Coin:      "xyz:CL",
		State:     map[string]float64{"toxicity": 0.4},
		Knobs:     map[string]float64{"spreadDeltaBuy": 1.5},
	})
	d.RecordRefresh(RefreshAttribution{
		Timestamp: time.Now(),
		Coin:      "xyz:CL",
		Trigger:   "post_fill",
		Action:    "refresh",
		Orders:    3,
	})
	d.RecordFill(FillRecord{
		Timestamp: time.Now(),
		Coin:      "xyz:CL",
		Side:      "buy",
		Size:      1.0,
		Price:     100.0,
		Notional:  100.0,
		NetPnL:    0.5,
	})

	finalState := map[string]any{
		"start_equity": 12000.0,
		"end_equity":   11952.0,
		"duration_sec": 1800.0,
		"supervisor":   "Flatten",
	}
	path, err := d.Dump("xyz:CL", "session_stop_loss", finalState)
	if err != nil {
		t.Fatalf("Dump failed: %v", err)
	}
	if path == "" {
		t.Fatal("Dump returned empty path")
	}

	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read dump: %v", err)
	}
	var payload map[string]any
	if err := json.Unmarshal(data, &payload); err != nil {
		t.Fatalf("unmarshal dump: %v", err)
	}
	for _, key := range []string{"coin", "reason", "timestamp", "session", "final_state", "last_evals", "last_refreshes", "fills", "per_side_stats"} {
		if _, ok := payload[key]; !ok {
			t.Errorf("dump missing required field %q", key)
		}
	}
	if got, _ := payload["coin"].(string); got != "xyz:CL" {
		t.Errorf("coin = %q, want xyz:CL", got)
	}
	if got, _ := payload["reason"].(string); got != "session_stop_loss" {
		t.Errorf("reason = %q, want session_stop_loss", got)
	}
	session, _ := payload["session"].(map[string]any)
	if session == nil {
		t.Fatal("session block missing")
	}
	if got, _ := session["session_pnl_bps"].(float64); got >= 0 {
		t.Errorf("session_pnl_bps = %v, want negative", got)
	}
	fills, _ := payload["fills"].([]any)
	if len(fills) != 1 {
		t.Errorf("fills count = %d, want 1", len(fills))
	}
}

func TestPostMortemDumper_RingBufferBoundsEvals(t *testing.T) {
	cfg := newTestPostMortemConfig(t)
	cfg.IncludeLastNEvals = 3
	d := NewPostMortemDumper(cfg)

	for i := 0; i < 10; i++ {
		d.RecordEval(EvalSnapshot{Coin: "x", EvalCount: i})
	}
	path, err := d.Dump("x", "test", nil)
	if err != nil {
		t.Fatalf("Dump: %v", err)
	}
	data, _ := os.ReadFile(path)
	var payload map[string]any
	_ = json.Unmarshal(data, &payload)
	evals, _ := payload["last_evals"].([]any)
	if len(evals) != 3 {
		t.Errorf("evals count = %d, want 3", len(evals))
	}
	first, _ := evals[0].(map[string]any)
	last, _ := evals[len(evals)-1].(map[string]any)
	if got, _ := first["eval_count"].(float64); int(got) != 7 {
		t.Errorf("first eval_count = %v, want 7 (oldest of last 3)", got)
	}
	if got, _ := last["eval_count"].(float64); int(got) != 9 {
		t.Errorf("last eval_count = %v, want 9 (newest)", got)
	}
}

func TestPostMortemDumper_RingBufferBoundsRefreshes(t *testing.T) {
	cfg := newTestPostMortemConfig(t)
	cfg.IncludeLastNRefreshes = 4
	d := NewPostMortemDumper(cfg)

	for i := 0; i < 12; i++ {
		d.RecordRefresh(RefreshAttribution{Coin: "x", Orders: i})
	}
	path, _ := d.Dump("x", "test", nil)
	data, _ := os.ReadFile(path)
	var payload map[string]any
	_ = json.Unmarshal(data, &payload)
	rs, _ := payload["last_refreshes"].([]any)
	if len(rs) != 4 {
		t.Fatalf("refreshes count = %d, want 4", len(rs))
	}
}

func TestPostMortemDumper_Rotation(t *testing.T) {
	cfg := newTestPostMortemConfig(t)
	cfg.RotateKeepCount = 2
	d := NewPostMortemDumper(cfg)

	for i := 0; i < 5; i++ {
		// Vary reason so filenames are unique even when the second-resolution
		// timestamp collides.
		if _, err := d.Dump("x", "r"+string(rune('0'+i)), nil); err != nil {
			t.Fatalf("Dump %d: %v", i, err)
		}
		// Ensure mtime ordering survives sub-millisecond OS resolution.
		time.Sleep(10 * time.Millisecond)
	}

	entries, _ := os.ReadDir(cfg.OutputDir)
	count := 0
	for _, e := range entries {
		if strings.HasSuffix(e.Name(), ".json") {
			count++
		}
	}
	if count != 2 {
		t.Errorf("file count after rotation = %d, want 2", count)
	}
}

func TestPostMortemDumper_FilenameTemplating(t *testing.T) {
	now := time.Date(2026, 4, 13, 15, 30, 0, 0, time.UTC)
	got := formatFilename("{coin}-{YYYYMMDD}-{HHMMSS}-{reason}.json", "xyz:CL", "session_stop_loss", now)
	want := "xyz_CL-20260413-153000-session_stop_loss.json"
	if got != want {
		t.Errorf("formatFilename = %q, want %q", got, want)
	}

	got2 := formatFilename("", "", "", now)
	if !strings.Contains(got2, "20260413") {
		t.Errorf("empty format default missing date: %q", got2)
	}

	got3 := formatFilename("{coin}.json", "../etc/passwd", "x", now)
	if strings.Contains(got3, "..") || strings.Contains(got3, "/") {
		t.Errorf("formatFilename did not sanitize traversal: %q", got3)
	}
}

func TestPostMortemDumper_DisabledIsNoop(t *testing.T) {
	dir := t.TempDir()
	d := NewPostMortemDumper(config.PostMortemConfig{Enabled: false, OutputDir: dir})
	if d.Enabled() {
		t.Fatal("dumper should be disabled")
	}
	d.RecordEval(EvalSnapshot{})
	d.RecordRefresh(RefreshAttribution{})
	d.RecordFill(FillRecord{})
	path, err := d.Dump("x", "test", nil)
	if err != nil {
		t.Fatalf("Dump returned error: %v", err)
	}
	if path != "" {
		t.Errorf("disabled Dump returned path %q, want empty", path)
	}
	entries, _ := os.ReadDir(dir)
	if len(entries) != 0 {
		t.Errorf("disabled Dump created %d files, want 0", len(entries))
	}
}

func TestPostMortemDumper_NilReceiverSafe(t *testing.T) {
	var d *PostMortemDumper
	if d.Enabled() {
		t.Error("nil dumper should not be Enabled()")
	}
	d.RecordEval(EvalSnapshot{})
	d.RecordRefresh(RefreshAttribution{})
	d.RecordFill(FillRecord{})
	path, err := d.Dump("x", "test", nil)
	if err != nil || path != "" {
		t.Errorf("nil Dump = (%q, %v), want (\"\", nil)", path, err)
	}
}

func TestPostMortemDumper_ConcurrentRecordAndDump(t *testing.T) {
	cfg := newTestPostMortemConfig(t)
	d := NewPostMortemDumper(cfg)

	var wg sync.WaitGroup
	stop := make(chan struct{})

	wg.Add(3)
	go func() {
		defer wg.Done()
		for {
			select {
			case <-stop:
				return
			default:
				d.RecordFill(FillRecord{Coin: "x", Side: "buy", Size: 1, Price: 100, Notional: 100})
			}
		}
	}()
	go func() {
		defer wg.Done()
		for {
			select {
			case <-stop:
				return
			default:
				d.RecordEval(EvalSnapshot{Coin: "x"})
			}
		}
	}()
	go func() {
		defer wg.Done()
		defer close(stop)
		for i := 0; i < 3; i++ {
			_, err := d.Dump("x", "concurrent", nil)
			if err != nil {
				t.Errorf("Dump: %v", err)
				return
			}
			time.Sleep(5 * time.Millisecond)
		}
	}()
	wg.Wait()
}

func TestPostMortemDumper_PerSideStats(t *testing.T) {
	cfg := newTestPostMortemConfig(t)
	d := NewPostMortemDumper(cfg)
	d.RecordFill(FillRecord{Coin: "x", Side: "buy", Notional: 100, NetPnL: 1})
	d.RecordFill(FillRecord{Coin: "x", Side: "buy", Notional: 200, NetPnL: 2})
	d.RecordFill(FillRecord{Coin: "x", Side: "sell", Notional: 50, NetPnL: -1})
	path, _ := d.Dump("x", "test", nil)
	data, _ := os.ReadFile(path)
	var payload map[string]any
	_ = json.Unmarshal(data, &payload)
	stats, _ := payload["per_side_stats"].(map[string]any)
	buy, _ := stats["buy"].(map[string]any)
	sell, _ := stats["sell"].(map[string]any)
	if got, _ := buy["count"].(float64); got != 2 {
		t.Errorf("buy.count = %v, want 2", got)
	}
	if got, _ := buy["notional"].(float64); got != 300 {
		t.Errorf("buy.notional = %v, want 300", got)
	}
	if got, _ := sell["count"].(float64); got != 1 {
		t.Errorf("sell.count = %v, want 1", got)
	}
}

func TestPostMortemDumper_FiltersFillsByCoin(t *testing.T) {
	cfg := newTestPostMortemConfig(t)
	d := NewPostMortemDumper(cfg)
	d.RecordFill(FillRecord{Coin: "a", Side: "buy", Notional: 100})
	d.RecordFill(FillRecord{Coin: "b", Side: "buy", Notional: 200})
	path, _ := d.Dump("a", "test", nil)
	data, _ := os.ReadFile(path)
	var payload map[string]any
	_ = json.Unmarshal(data, &payload)
	fills, _ := payload["fills"].([]any)
	if len(fills) != 1 {
		t.Fatalf("fills count = %d, want 1", len(fills))
	}
	first, _ := fills[0].(map[string]any)
	if got, _ := first["coin"].(string); got != "a" {
		t.Errorf("filtered fill coin = %q, want a", got)
	}
}

func TestPostMortemDumper_OutputDirCreated(t *testing.T) {
	base := t.TempDir()
	dir := filepath.Join(base, "nested", "postmortem")
	cfg := config.PostMortemConfig{
		Enabled:         true,
		OutputDir:       dir,
		FilenameFormat:  "{coin}.json",
		RotateKeepCount: 10,
	}
	d := NewPostMortemDumper(cfg)
	if _, err := d.Dump("x", "test", nil); err != nil {
		t.Fatalf("Dump: %v", err)
	}
	if _, err := os.Stat(dir); err != nil {
		t.Errorf("output dir not created: %v", err)
	}
}
