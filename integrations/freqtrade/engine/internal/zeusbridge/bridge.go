// Package zeusbridge exports bounded, secret-free current state over stdout.
// It never writes journals, configuration or credentials.
package zeusbridge

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"math"
	"os"
	"strconv"
	"strings"
	"sync"
	"time"
)

type State struct {
	mu            sync.Mutex
	out           io.Writer
	session, coin string
	start         int64
	seen          map[string]bool
	data          map[string]any
}

var active *State

func (s *State) StartMillis() int64 { return s.start }

func Configure(session, coin string) *State {
	s := &State{out: os.Stdout, session: session, coin: coin, start: time.Now().UnixMilli(), seen: map[string]bool{}, data: map[string]any{
		"phase": "connecting", "running": true, "position_known": false, "volume": 0.0, "fees": 0.0, "rpnl": 0.0, "fills": int64(0),
		"order_placements": int64(0), "order_modifications": int64(0), "order_cancellations": int64(0), "order_requests": int64(0),
	}}
	active = s
	return s
}
func Number(v any) float64 { f, _ := strconv.ParseFloat(fmt.Sprint(v), 64); return f }
func (s *State) Update(values map[string]any) {
	if s == nil {
		return
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	for k, v := range values {
		s.data[k] = v
	}
}
func (s *State) Emit() {
	if s == nil {
		return
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	values := map[string]any{"protocol": 1, "session_id": s.session, "symbol": s.coin, "ts": float64(time.Now().UnixMilli()) / 1000}
	for k, v := range s.data {
		values[k] = v
	}
	volume := s.data["volume"].(float64)
	values["cost_per_1m"] = nil
	if volume > 0 && s.data["accounting_complete"] != false {
		values["cost_per_1m"] = (s.data["fees"].(float64) - s.data["rpnl"].(float64)) * 1e6 / volume
	}
	_ = json.NewEncoder(s.out).Encode(values)
}
func RecordFill(raw map[string]any) {
	s := active
	if s == nil {
		return
	}
	coin := fmt.Sprint(raw["coin"])
	if coin != s.coin && coin != strings.SplitN(s.coin, ":", 2)[len(strings.SplitN(s.coin, ":", 2))-1] {
		return
	}
	if raw["tid"] == nil || Number(raw["time"]) < float64(s.start) {
		return
	}
	if token, ok := raw["feeToken"].(string); ok && token != "USDC" {
		return
	}
	values := make([]float64, 4)
	for i, key := range []string{"px", "sz", "fee", "closedPnl"} {
		value, err := strconv.ParseFloat(fmt.Sprint(raw[key]), 64)
		if err != nil {
			return
		}
		values[i] = value
	}
	px, qty, fee, pnl := values[0], values[1], values[2], values[3]
	if raw["closedPnl"] == nil || raw["fee"] == nil || px <= 0 || qty <= 0 || math.IsNaN(px+qty+fee+pnl) || math.IsInf(px+qty+fee+pnl, 0) {
		return
	}
	// Normalize bare/qualified coin names so REST and WS share one identity.
	key := s.coin + ":" + fmt.Sprint(raw["tid"])
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.seen[key] {
		return
	}
	s.seen[key] = true
	s.data["volume"] = s.data["volume"].(float64) + px*qty
	s.data["fees"] = s.data["fees"].(float64) + fee + Number(raw["builderFee"])
	s.data["rpnl"] = s.data["rpnl"].(float64) + pnl
	s.data["fills"] = s.data["fills"].(int64) + 1
}
func RecordAction(kind string, count int) {
	s := active
	if s == nil {
		return
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	key := map[string]string{"order": "order_placements", "batchModify": "order_modifications", "cancel": "order_cancellations"}[kind]
	if key == "" {
		return
	}
	s.data[key] = s.data[key].(int64) + int64(count)
	s.data["order_requests"] = s.data["order_requests"].(int64) + 1
}

// ErrorHandler only forwards error messages, never startup/config/account dumps.
type ErrorHandler struct{ State *State }

const maxZeusErrorLen = 240

func (h ErrorHandler) Enabled(_ context.Context, l slog.Level) bool { return l >= slog.LevelError }
func (h ErrorHandler) Handle(_ context.Context, r slog.Record) error {
	// Attrs carry the actionable context ("error=...", "reason=...", coin).
	// Dropping them reduced the Zeus banner to a bare sentence with no way to
	// tell a cancelled-order race from an outage. Keep the message text as the
	// stable key; append attr values after it, capped for UI display.
	msg := r.Message
	var details []string
	r.Attrs(func(a slog.Attr) bool {
		if a.Value.Kind() != slog.KindGroup {
			details = append(details, fmt.Sprintf("%s=%v", a.Key, a.Value))
		}
		return true
	})
	if len(details) > 0 {
		msg += ": " + strings.Join(details, ", ")
	}
	if runes := []rune(msg); len(runes) > maxZeusErrorLen {
		msg = string(runes[:maxZeusErrorLen])
	}
	h.State.Update(map[string]any{"error": msg})
	h.State.Emit()
	return nil
}
func (h ErrorHandler) WithAttrs(_ []slog.Attr) slog.Handler { return h }
func (h ErrorHandler) WithGroup(_ string) slog.Handler      { return h }
