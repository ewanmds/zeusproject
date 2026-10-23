package main

import (
	"fmt"
	"log/slog"
	"strings"

	"github.com/quiknode-labs/hyperliquid-sdk/go/hyperliquid"
	"hyperliquid-market-maker/internal/config"
	"hyperliquid-market-maker/internal/types"
)

func validateAgentRole(account, signer string, role map[string]any) error {
	if account == "" || signer == "" || strings.EqualFold(account, signer) {
		return fmt.Errorf("use a separate authorized API wallet key and the main account address")
	}
	data, _ := role["data"].(map[string]any)
	owner, _ := data["user"].(string)
	if role["role"] != "agent" || !strings.EqualFold(owner, account) {
		return fmt.Errorf("API wallet is not authorized for WALLET_ADDRESS; authorize it in Hyperliquid API settings")
	}
	return nil
}

// checkTradingAccount only calls Info endpoints. It must run before leverage,
// order creation, cancellation, or any other exchange action.
func checkTradingAccount(sdk *hyperliquid.SDK, cfg *config.Config) error {
	role, err := sdk.Info().UserRole(sdk.SignerAddress())
	if err != nil {
		return fmt.Errorf("query API wallet authorization: %w", err)
	}
	if err := validateAgentRole(cfg.WalletAddress, sdk.SignerAddress(), role); err != nil {
		return err
	}
	slog.Info("API wallet authorized", "account", sdk.Address(), "signer", sdk.SignerAddress())
	dexes := map[string]bool{}
	for _, asset := range cfg.Strategy.Assets {
		dex, _ := types.ParseAsset(asset)
		dexes[dex] = true
	}
	for dex := range dexes {
		state, err := sdk.Info().ClearinghouseState(sdk.Address(), hyperliquid.WithDex(dex))
		if err != nil {
			return fmt.Errorf("query account balance for dex %q: %w", dex, err)
		}
		summary, _ := state["marginSummary"].(map[string]any)
		orders, err := sdk.Info().OpenOrders(sdk.Address(), hyperliquid.WithDex(dex))
		if err != nil {
			return fmt.Errorf("query open orders for dex %q: %w", dex, err)
		}
		slog.Info("account state (read-only)", "account", sdk.Address(), "dex", dex, "account_value", summary["accountValue"], "open_orders", len(orders))
	}
	slog.Info("direct Hyperliquid execution; no builder field or builder fee; exchange trading fees still apply")
	return nil
}
