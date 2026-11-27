Add a "drone" shipping method to shipping_cost in shop/shipping.py: 12.0 plus
2.0 per kg, at most 5 kg (raise ValueError above that). Express applies as for
the others. Add a test, and make ./check.sh pass. It is the CI gate.
