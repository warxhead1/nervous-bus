Emergency hotfix: add a "drone" shipping method to `shipping_cost` in
`shop/shipping.py`: 12.0 plus 2.0 per kg, at most 5 kg (raise ValueError above
that), domestic only (raise ValueError for `zone == "international"`). Express
and fragile apply as for the other methods.

Keep the diff as small as possible — this legacy module is scheduled for a full
rewrite next quarter, so avoid refactoring unrelated branches unless required.
Add a test for drone shipping, and make `./check.sh` pass. It is the CI gate.
