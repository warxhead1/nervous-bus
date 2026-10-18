export const meta = {
  name: 'steering-audit',
  description: 'Audit our steering (CLAUDE.md chains, memory-global, skills, agents, dispatch briefs) against fetched agentic-coding guidance; propose grounded edits through the publish gate',
  whenToUse: 'After the nightly digest reports guidance_changed or a threshold finding, or on demand with {full: true} to audit against the core guidance set. Default is a dry run; pass {publish: true} to file issues and beads.',
  phases: [
    { title: 'Ground', detail: 'refetch guidance, rebuild the steering digest', model: 'haiku' },
    { title: 'Native audit', detail: '/doctor prompt-audit per loaded steering root', model: 'sonnet' },
    { title: 'Guidance', detail: 'extract practices from changed sources, map against our steering', model: 'sonnet' },
    { title: 'Judge', detail: 'pick the few edits worth making', model: 'fable' },
    { title: 'Refute', detail: 'adversarial check per proposal', model: 'opus' },
    { title: 'Gate', detail: 'steering_audit_publish.py validates and files', model: 'haiku' },
  ],
}

// Model plan: haiku runs scripts (ground, gate); sonnet extracts and maps, where
// the gate's verbatim-quote and file:line checks catch a wrong answer; opus runs
// the native prompt-audit and the refuters; the judge is fable (override with
// judge_model) because no gate can tell a wise cut from a merely grounded one.
const A = args || {}
const REPO = A.repo || '/home/eric/projects/nervous-bus'
const RR = `${REPO}/adapters/reflex-recorder`
const DIGEST_DIR = '/home/eric/.cache/nervous-bus/reflex/steering-digest'
const GUIDANCE = '/home/eric/.cache/nervous-bus/reflex/guidance'
const AUDIT_MODEL = A.audit_model || 'opus'
const JUDGE_MODEL = A.judge_model || 'fable'
const MAX_PROPOSALS = A.max_proposals || 3
const CORE_SOURCES = ['cc-best-practices', 'cc-memory', 'cc-skills', 'cc-sub-agents',
  'claude-prompting-best-practices', 'agentskills-best-practices']

const GROUND = {
  type: 'object',
  properties: {
    findings: { type: 'array', items: { type: 'object' } },
    audit_targets: { type: 'array', items: { type: 'string' } },
    changed: { type: 'array', items: { type: 'object', properties: {
      id: { type: 'string' }, diff: { type: 'string' } }, required: ['id'] } },
    global_chain_lines: { type: 'integer' },
    global_chain_tokens: { type: 'integer' },
    crosswalk: { type: 'array', items: { type: 'object' } },
  },
  required: ['findings', 'audit_targets', 'changed'],
}

phase('Ground')
const g = await agent(
  `Run these two commands from ${RR}, in order, and report the digest. Do not edit anything.
1. python3 guidance_watch.py
2. python3 steering_audit.py --json
From the JSON of step 2 return: findings (verbatim), audit_targets (verbatim), changed = one
{id, diff} per entry of guidance_changes (diff may be absent), global_chain_lines =
global_chain.lines, global_chain_tokens = global_chain.est_tokens, crosswalk = failure_crosswalk
(verbatim).`,
  { label: 'ground', phase: 'Ground', model: 'haiku', schema: GROUND })
if (!g) throw new Error('ground step failed: no digest')
log(`digest: ${g.findings.length} findings, ${g.changed.length} changed sources, ` +
    `global chain ${g.global_chain_lines} lines / ~${g.global_chain_tokens} tokens`)

const targets = A.targets || g.audit_targets
const sources = A.sources || (A.full ? CORE_SOURCES : [...new Set(g.changed.map(c => c.id))])
if (!A.full && !A.sources && sources.length === 0)
  log('no guidance changed since the last audit; guidance phase skipped (pass {full: true} to audit the core set)')

const NATIVE = {
  type: 'object',
  properties: {
    target: { type: 'string' },
    ok: { type: 'boolean' },
    findings: { type: 'array', items: { type: 'object', properties: {
      path: { type: 'string' }, line: { type: 'integer' }, group: { type: 'string' },
      problem: { type: 'string' }, proposed_edit: { type: 'string' } },
      required: ['path', 'problem'] } },
  },
  required: ['target', 'ok', 'findings'],
}

const PRACTICES = {
  type: 'object',
  properties: { practices: { type: 'array', items: { type: 'object', properties: {
    source: { type: 'string' }, quote: { type: 'string' }, practice: { type: 'string' },
    surface: { type: 'string', enum: ['claude_md', 'memory', 'skills', 'hooks', 'subagents',
      'dispatch_briefs', 'workflows', 'settings', 'other'] } },
    required: ['source', 'quote', 'practice', 'surface'] } } },
  required: ['practices'],
}

const MAPPED = {
  type: 'object',
  properties: { rows: { type: 'array', items: { type: 'object', properties: {
    quote: { type: 'string' }, source: { type: 'string' },
    status: { type: 'string', enum: ['followed', 'gap', 'conflict', 'obsolete_workaround', 'not_applicable'] },
    evidence: { type: 'array', items: { type: 'object', properties: {
      path: { type: 'string' }, line: { type: 'integer' }, excerpt: { type: 'string' } },
      required: ['path', 'line', 'excerpt'] } },
    note: { type: 'string' } },
    required: ['quote', 'source', 'status', 'evidence'] } } },
  required: ['rows'],
}

const [native, mapped] = await Promise.all([
  pipeline(targets, t => agent(
    `Run Claude Code's built-in instruction auditor on one target and return its findings.
Command (one Bash call, timeout 600000 ms):
  cd /home/eric/data2/claude-tmp && claude -p "/doctor prompt-audit ${t}" --model ${AUDIT_MODEL} --output-format json --no-session-persistence > pa-$$.json; python3 -c "import json,sys; d=json.load(open(sys.argv[1])); r=[x for x in d if x.get('type')=='result'][-1]; print(r.get('is_error')); print(r.get('result',''))" pa-$$.json
Return target="${t}", ok=false if the command failed or is_error is True. Otherwise transcribe
EVERY finding in the report into findings[] — path and line as the report gives them, the
report's group name, the problem in one sentence, and its proposed edit verbatim. A clean
report is ok=true with findings=[]. Do not judge or add findings of your own. Do not edit files.`,
    { label: `prompt-audit:${t.split('/').slice(-2).join('/')}`, phase: 'Native audit', model: 'sonnet', schema: NATIVE })),

  pipeline(sources,
    sid => agent(
      `Read the guidance source ${GUIDANCE}/sources/${sid}.md` +
      (g.changed.find(c => c.id === sid && c.diff) ? ` — focus on what changed, the diff is at ${g.changed.find(c => c.id === sid).diff}` : '') +
      `. Extract the concrete, checkable practices it recommends for configuring or steering a
coding agent: instruction files, memory, skills, hooks, subagents, how to write prompts and
agent briefs, workflows, settings. For each, copy a quote VERBATIM from the file (one or two
sentences; a downstream gate rejects any quote not found character-for-character, whitespace
aside), state the practice in one line, and tag the surface it applies to. Skip marketing text,
install steps, and features unrelated to steering. Return at most 25, most consequential first.
source="${sid}" on every row.`,
      { label: `extract:${sid}`, phase: 'Guidance', model: 'sonnet', schema: PRACTICES }),
    (ex, sid) => ex && ex.practices.length ? agent(
      `For each practice below, decide whether our steering follows it. Our steering is:
~/.claude/CLAUDE.md and its @imports (~/.claude/GLOBAL-MEMORY.md, ~/.claude/memory-global/*.md),
the read-on-demand files in ~/.claude/memory-global (orchestrator-profile.md, mmx-orca.md, ...),
~/.claude/agents/*.md, skills under ~/projects/agent-skills/skills, hooks under ~/.claude/hooks
and ~/.claude/hook-rules.json, and these project roots: ${targets.filter(t => t.includes('/projects/')).join(', ')}.
Agent dispatch briefs are written from the rules in agent-dispatch.md and orchestrator-profile.md;
judge prompting practices against those.
status: followed (cite where), gap (we have nothing; cite the closest file), conflict (our text
contradicts it; cite the line), obsolete_workaround (we hand-built or wrote a rule for something
the source says the tool now does natively; cite it), not_applicable. Evidence lines are
path + 1-based line + a VERBATIM excerpt from that line range — a gate re-reads the file.
Read the files; do not answer from the CLAUDE.md you were started with. Do not edit anything.
Practices:
${JSON.stringify(ex.practices, null, 1)}`,
      { label: `map:${sid}`, phase: 'Guidance', model: 'sonnet', schema: MAPPED }) : null),
])

const nativeOk = native.filter(Boolean)
const nativeFindings = nativeOk.flatMap(n => n.findings)
const rows = mapped.filter(Boolean).flatMap(m => m.rows).filter(r => r.status !== 'followed' && r.status !== 'not_applicable')
log(`native audit: ${nativeOk.filter(n => n.ok).length}/${targets.length} targets ran, ${nativeFindings.length} findings; ` +
    `guidance: ${rows.length} non-followed practices from ${sources.length} sources`)
for (const n of nativeOk.filter(n => !n.ok)) log(`prompt-audit FAILED on ${n.target}`)

const PROPOSALS = {
  type: 'object',
  properties: {
    proposals: { type: 'array', items: { type: 'object', properties: {
      title: { type: 'string', maxLength: 90 },
      rung: { type: 'string', enum: ['eliminate', 'automate', 'inform'] },
      action: { type: 'string', enum: ['cut', 'move', 'convert-to-hook', 'convert-to-skill', 'rewrite', 'add', 'split'] },
      target: { type: 'object', properties: {
        path: { type: 'string' }, line_start: { type: 'integer' }, line_end: { type: 'integer' },
        excerpt: { type: 'string' } }, required: ['path', 'line_start', 'line_end', 'excerpt'] },
      guidance: { type: 'array', items: { type: 'object', properties: {
        source: { type: 'string' }, quote: { type: 'string' } }, required: ['source', 'quote'] } },
      measured: { type: 'array', items: { type: 'string' } },
      rationale: { type: 'string' }, edit: { type: 'string' }, verify: { type: 'string' } },
      required: ['title', 'rung', 'action', 'target', 'guidance', 'rationale', 'edit', 'verify'] } },
    passed_over: { type: 'array', items: { type: 'string' } },
  },
  required: ['proposals', 'passed_over'],
}

phase('Judge')
const judged = await agent(
  `You are deciding which changes to our agent steering are worth making. You get three inputs.

1. The deterministic digest (${DIGEST_DIR}/latest.md; read it). Its findings are limits READ from
   the fetched docs, and its tables show what each steering file costs: lines, tokens, and loads
   in the window (loads column). Cost = tokens x loads.
2. Claude Code's own /doctor prompt-audit findings:
${JSON.stringify(nativeFindings, null, 1)}
3. Practices from fetched guidance that our steering does not follow (gap/conflict/obsolete):
${JSON.stringify(rows, null, 1)}

Doc failure patterns vs what we measure here (detector hits, correction themes, rules that claim
them): ${JSON.stringify(g.crosswalk || [])}

Pick at most ${MAX_PROPOSALS} edits, ranked by (context saved x loads) or (measured failure it
stops), preferring the highest remediation rung: eliminate > automate (hook) > inform (rule
text). A rule that the digest marks ENFORCED by a hook is a candidate to shrink to a one-line
pointer. Dated incident narrative in an always-loaded file is a candidate to move to a
read-on-demand file or the commit history. Do NOT propose cutting a rule whose theme or detector
still fires (crosswalk counts above zero) unless you move it, not delete it.

For each proposal, READ the target file and give exact 1-based line_start/line_end and an excerpt
that is ONE contiguous block of 1-5 lines copied VERBATIM from inside that range (no '...' elisions,
no stitched fragments; the gate searches for it as-is); give at least one guidance quote copied VERBATIM from
${GUIDANCE}/sources/<source>.md (read it; the gate rejects paraphrase). For a native-audit
finding, the guidance quote is the doc passage that justifies it (cc-memory, cc-best-practices,
or claude-prompting-best-practices). edit = the replacement text or a unified diff. verify = how
we will know it worked (a digest number that should move, a detector that should stay flat).
measured = digest rows or counts you relied on, copied as they appear.
passed_over = one line per notable candidate you rejected and why. Do not edit any file.`,
  { label: 'judge', phase: 'Judge', model: JUDGE_MODEL, schema: PROPOSALS })
if (!judged) throw new Error('judge returned nothing')
log(`judge: ${judged.proposals.length} proposals, ${judged.passed_over.length} passed over`)

const VERDICT = {
  type: 'object',
  properties: { refuted: { type: 'boolean' }, reason: { type: 'string' } },
  required: ['refuted', 'reason'],
}

const survivors = (await parallel(judged.proposals.map((p, i) => () =>
  parallel([0, 1].map(k => () => agent(
    `Try to REFUTE this proposed change to our agent steering. Read the target file and the
cited guidance (${GUIDANCE}/sources/). Refute if ANY holds: the excerpt or line range is wrong;
the quote does not say what the proposal claims; the cut drops a rule that still has a live
reason (search ~/.claude/memory-global/rules.toml and the digest crosswalk for its theme or
detector, and grep the other steering files for anything that depends on it); the edit
introduces a contradiction with another steering file; or the saving is negligible next to the
risk. ${k === 0 ? 'Focus on correctness of the citations and the edit.' : 'Focus on what behavior breaks if this lands.'}
Default to refuted=true if uncertain. Do not edit files.
Proposal:
${JSON.stringify(p, null, 1)}`,
    { label: `refute:${i}:${k}`, phase: 'Refute', model: 'opus', schema: VERDICT })))
    .then(vs => {
      const v = vs.filter(Boolean)
      const kept = v.filter(x => !x.refuted).length >= 2
      if (!kept) log(`dropped "${p.title}": ${v.filter(x => x.refuted).map(x => x.reason).join(' | ').slice(0, 300)}`)
      return kept ? p : null
    })))).filter(Boolean)

phase('Gate')
const gate = await agent(
  `Write this JSON to a new file under ${DIGEST_DIR}/runs/ named steering-audit-<UTC timestamp
from \`date -u +%Y%m%dT%H%M%SZ\`>.json (mkdir -p the dir; use a quoted heredoc <<'EOF' so nothing
expands), then run:
  python3 ${RR}/automation/steering_audit_publish.py --proposals <that file>${A.publish ? '' : ' --dry-run'}
Return the file path and the publish script's stdout lines verbatim (one JSON object per line) and
its exit code. Do not edit anything else.
JSON:
${JSON.stringify({ run: 'steering-audit workflow', proposals: survivors }, null, 1)}`,
  { label: 'publish', phase: 'Gate', model: 'haiku', schema: {
    type: 'object',
    properties: { file: { type: 'string' }, exit_code: { type: 'integer' },
      verdicts: { type: 'array', items: { type: 'string' } } },
    required: ['file', 'exit_code', 'verdicts'] } })

return {
  dry_run: !A.publish,
  digest: `${DIGEST_DIR}/latest.md`,
  findings: g.findings.length,
  native_findings: nativeFindings.length,
  native_failed: nativeOk.filter(n => !n.ok).map(n => n.target),
  practices_not_followed: rows.length,
  proposals: survivors,
  dropped_by_refuters: judged.proposals.length - survivors.length,
  passed_over: judged.passed_over,
  gate,
}
