# falsify-your-gate

A check counts only after you have seen it fail on the bad case. Break the code or plant the defect, watch it go red, restore. Assert on what the code produced, not on a constant.

A green result means nothing until you know the check ran and can go red: count what it ran, run it on a known-bad case, and run it on known-good work to see how often it cries wolf. A threshold comes from measured data. If a fix changed the test, the assertions and the thing it measures must still be there.
