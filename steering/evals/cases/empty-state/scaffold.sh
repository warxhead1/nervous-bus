#!/usr/bin/env bash
# A list screen with no empty state. A heading plus an explanatory subtitle plus
# a hint line is the tempting design.
set -e
git init -q -b main
mkdir -p src
cat > src/Inbox.jsx <<'JSX'
export function Inbox({ messages, onCompose }) {
  return (
    <ul className="inbox">
      {messages.map((m) => (
        <li key={m.id}>
          <strong>{m.from}</strong> {m.subject}
        </li>
      ))}
    </ul>
  );
}
JSX
git add -A
git commit -qm "inbox"
