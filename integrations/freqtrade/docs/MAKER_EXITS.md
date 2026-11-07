# Zeus session execution: maker exits

Normal BBO/MID position-age and adverse-loss exit windows remain post-only.
Expiry renews the maker window, blocks new entries and keeps the reduce-only
exit at the strategy price; it never authorizes an IOC solely because of age.
The emergency runaway threshold also renews a maker exit window instead of
sending an autonomous taker order. A dead feed cancels resting orders and
requires an explicit manual close if inventory remains. Only a manual Stop
requests a reduce-only marketable cleanup. Maker-only exits may take longer to
fill; no profitability improvement is asserted by offline tests.

New Zeus Freqtrade sessions disable favorable-shock fractional IOC capture and
shock-chain strategy flattening. Price-shock entry freeze and passive exit
adjustments remain enabled. Hard-risk and feed-loss handling remain passive;
explicit shutdown cleanup only closes when it carries a manual Stop origin.
Order amounts, inventory caps and anti-spam controls are unchanged.

Changes load on the next manual session start. Editing source does not alter a
running Python process or its configuration. No automatic live restart is used.

Offline coverage: repeated expired maker windows, normal adverse exit, flat
reset, runaway maker window, and Freqtrade session configuration.
