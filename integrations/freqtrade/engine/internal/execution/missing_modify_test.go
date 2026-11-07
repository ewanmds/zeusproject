package execution

import (
	"context"
	"errors"
	"github.com/quiknode-labs/hyperliquid-sdk/go/hyperliquid"
	"hyperliquid-market-maker/internal/types"
	"testing"
)

func TestMissingModifyContinuesOnlyAfterConfirmedAbsence(t *testing.T) {
	for _, scenario := range []string{"filled", "canceled", "still_open", "unavailable"} {
		t.Run(scenario, func(t *testing.T) {
			le := safetyExecutor()
			le.cfg.Risk.MaxPositionNotional = 1000
			le.positions = map[string]*livePosition{}
			le.szDecimals = map[string]int{"io:ANTH": 3}
			sends, reads := 0, 0
			send := func(int64, string, string, string, string, ...hyperliquid.ModifyOption) (*hyperliquid.PlacedOrder, error) {
				sends++
				return &hyperliquid.PlacedOrder{Status: "error: Cannot modify canceled or filled order"}, nil
			}
			reconcile := func(coin string, oid int64) (placeOrderResult, error) {
				reads++
				if coin != "io:ANTH" || oid != 12 {
					t.Fatal("wrong scope")
				}
				if scenario == "unavailable" {
					return placeOrderResult{}, errors.New("API unavailable")
				}
				return placeOrderResult{benignReject: true, originalGone: scenario != "still_open"}, nil
			}
			result, err := le.modifyWithCallbacks(context.Background(), "io:ANTH", &trackedOrder{OID: 12}, &types.OrderRequest{Coin: "io:ANTH", Side: "buy", Size: 0.1, Price: 1800}, send, reconcile)
			if sends != 1 || reads != 1 {
				t.Fatalf("unexpected retries %d %d", sends, reads)
			}
			if scenario == "filled" || scenario == "canceled" {
				if err != nil || le.SafetyError() != nil || !result.benignReject || !result.originalGone {
					t.Fatalf("normal race killed session: %+v %v", result, err)
				}
				release, gateErr := le.beginAction(context.Background())
				if gateErr != nil {
					t.Fatal(gateErr)
				}
				release()
			} else if err == nil || le.SafetyError() == nil {
				t.Fatal("unverified state permitted quoting")
			}
			le.Quiesce()
			if _, err := le.beginAction(context.Background()); err == nil {
				t.Fatal("Stop reopened")
			}
		})
	}
}
func TestMissingModifyClassificationIsPrecise(t *testing.T) {
	for _, message := range []string{"error: Cannot modify canceled or filled order", "error: Cannot modify canceled or filled order. asset=200001"} {
		if !isMissingModifyReject(&hyperliquid.PlacedOrder{Status: message}) {
			t.Fatal(message)
		}
	}
	for _, message := range []string{"error: permission denied", "error: Cannot modify canceled or filled order; insufficient margin", "resting"} {
		if isMissingModifyReject(&hyperliquid.PlacedOrder{Status: message}) {
			t.Fatal(message)
		}
	}
}
