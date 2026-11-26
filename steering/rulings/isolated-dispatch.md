# isolated-dispatch

Every agent that edits files gets its own worktree and its own branch; never two agents on one file. Check the tree is clean and the tests are green before dispatching. Each agent pushes its branch after its first commit; you do the merges.

Changes in the tree that you did not make are someone's work. Read them (`git diff`, `git log`) before anything else; never stash, reset, or check out over them. Name the branch you are on before a commit, merge or push.
