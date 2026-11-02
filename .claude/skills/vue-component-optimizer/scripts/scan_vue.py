#!/usr/bin/env python3
"""
Heuristic scanner for Vue 3 single-file components.

Produces an inventory of components and a list of *signals* worth a human (or
Claude) look: oversized components, unstable v-for keys, methods called from
templates, composables called outside setup's top level, duplicated helpers
across files, eager route/modal imports, dead code (scoped CSS classes the
template never uses, setup bindings returned but never referenced), and tags
that look like components but are neither imported nor registered. Signals are leads, not verdicts --
always read the code before reporting one as a finding.

Usage:
    python3 scan_vue.py <src-dir> [--json]
"""
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

# Sizes past which a component usually mixes several responsibilities
COMPONENT_LINES_WARN = 400
TEMPLATE_LINES_WARN = 200
SCRIPT_LINES_WARN = 250

BLOCK_RE = re.compile(r'^<(template|script|style)\b[^>]*>(.*?)^</\1>', re.S | re.M)
FUNC_DEF_RE = re.compile(r'^\s*(?:const|let)\s+([a-zA-Z_]\w*)\s*=\s*(?:async\s*)?\([^)]*\)\s*=>|^\s*(?:async\s+)?function\s+([a-zA-Z_]\w*)\s*\(', re.M)
COMPOSABLE_CALL_RE = re.compile(r'\buse[A-Z]\w*\s*\(')
INDEX_KEY_RE = re.compile(r':key="\s*(index|idx|i|n|key)\s*"')
V_FOR_RE = re.compile(r'<[a-zA-Z][^>]*\bv-for="[^"]*"[^>]*>', re.S)
MUSTACHE_RE = re.compile(r'\{\{(.*?)\}\}', re.S)
BINDING_RE = re.compile(r'\s((?::|v-bind:|v-if|v-else-if|v-show|v-for|v-html|v-text)[\w.-]*)="([^"]*)"')
CALL_RE = re.compile(r'\b([a-z][a-zA-Z0-9_]*)\(')
CLASS_SELECTOR_RE = re.compile(r'\.([a-zA-Z][\w-]*)')
RETURN_BLOCK_RE = re.compile(r'\n\s*return\s*\{([^{}]*)\}\s*\n\s*\}\s*\n?\s*\}?\s*$', re.S)
PASCAL_TAG_RE = re.compile(r'<([A-Z][A-Za-z0-9]+)[\s/>]')
# Vue built-ins and router components that never need registering
BUILTIN_TAGS = {'Teleport', 'Transition', 'TransitionGroup', 'KeepAlive', 'Suspense', 'Component', 'RouterView', 'RouterLink'}
STATIC_IMPORT_RE = re.compile(r"^import\s+(\w+)\s+from\s+'([^']+\.vue)'", re.M)

# Template calls that are cheap or unavoidable and not worth flagging
CHEAP_CALLS = {'t', '$t', 'emit', '$emit', 'String', 'Number', 'Math', 'parseInt', 'parseFloat'}


def line_of(text, index, offset=0):
    return text.count('\n', 0, index) + 1 + offset


def blocks(source):
    """Return {kind: (content, start_line)} for template/script/style."""
    found = {}
    for m in BLOCK_RE.finditer(source):
        kind = m.group(1)
        if kind not in found:
            found[kind] = (m.group(2), line_of(source, m.start(2)) - 1)
    return found


def setup_body(script):
    """Return (body, offset_in_script) of setup(), or the whole <script setup>."""
    m = re.search(r'\bsetup\s*\([^)]*\)\s*\{', script)
    if not m:
        return script, 0
    depth, i = 1, m.end()
    while i < len(script) and depth:
        depth += {'{': 1, '}': -1}.get(script[i], 0)
        i += 1
    return script[m.end():i - 1], m.end()


def scan_file(path, root):
    source = path.read_text(encoding='utf-8')
    rel = str(path.relative_to(root))
    parts = blocks(source)
    template, t_start = parts.get('template', ('', 0))
    script, s_start = parts.get('script', ('', 0))
    style, _ = parts.get('style', ('', 0))

    info = {
        'file': rel,
        'lines': source.count('\n') + 1,
        'template_lines': template.count('\n'),
        'script_lines': script.count('\n'),
        'style_lines': style.count('\n'),
        'functions': [],
        'signals': [],
    }

    def signal(kind, line, detail):
        info['signals'].append({'kind': kind, 'line': line, 'detail': detail})

    if info['lines'] > COMPONENT_LINES_WARN:
        signal('large-component', 1, f"{info['lines']} lines (template {info['template_lines']}, "
               f"script {info['script_lines']}, style {info['style_lines']})")
    if info['template_lines'] > TEMPLATE_LINES_WARN:
        signal('large-template', t_start + 1, f"template is {info['template_lines']} lines")
    if info['script_lines'] > SCRIPT_LINES_WARN:
        signal('large-script', s_start + 1, f"script is {info['script_lines']} lines")

    # v-for checks
    for m in V_FOR_RE.finditer(template):
        tag = m.group(0)
        line = line_of(template, m.start(), t_start)
        if ':key=' not in tag and 'v-bind:key=' not in tag:
            signal('v-for-no-key', line, 'v-for without :key')
        elif INDEX_KEY_RE.search(tag):
            signal('index-key', line, 'v-for keyed by array index')
        if 'v-if=' in tag:
            signal('v-for-with-v-if', line, 'v-if on the same element as v-for')

    # Functions defined in script, and which of them the template calls
    defined = {}
    for m in FUNC_DEF_RE.finditer(script):
        name = m.group(1) or m.group(2)
        defined[name] = line_of(script, m.start(), s_start)
    info['functions'] = sorted(defined)

    # Only render-time expressions count; @click handlers run on events, not renders
    calls = defaultdict(list)
    expressions = [(m.group(1), m.start(1)) for m in MUSTACHE_RE.finditer(template)]
    expressions += [(m.group(2), m.start(2)) for m in BINDING_RE.finditer(template)]
    for expr, start in expressions:
        for m in CALL_RE.finditer(expr):
            name = m.group(1)
            if name in defined and name not in CHEAP_CALLS:
                calls[name].append(line_of(template, start + m.start(1), t_start))
    for name, lines in sorted(calls.items()):
        # A function called from the template re-runs on every render; repeated
        # calls with the same arguments are the classic computed() candidate
        if len(lines) > 1:
            signal('template-method-calls', lines[0],
                   f"{name}() called {len(lines)}x from template (lines {', '.join(map(str, lines[:6]))})")

    # Composables must run synchronously at setup's top level; nested calls
    # create new instances per call and break when called after an await
    body, body_offset = setup_body(script)
    # Track function nesting only; braces of a multi-line `const { a, b } = useX()`
    # destructure are not a nested scope
    depth, in_destructure = 0, False
    for i, line in enumerate(body.split('\n')):
        stripped = line.strip()
        if in_destructure:
            if stripped.startswith('}'):
                in_destructure = False
            continue
        if re.match(r'(const|let)\s*\{[^}]*$', stripped):
            in_destructure = True
            continue
        if COMPOSABLE_CALL_RE.search(line) and depth > 0:
            signal('composable-in-function', line_of(script, body_offset, s_start) + i,
                   f"composable called inside a nested function: {stripped[:90]}")
        depth += line.count('{') - line.count('}')

    for m in re.finditer(r'deep\s*:\s*true', script):
        signal('deep-watch', line_of(script, m.start(), s_start), 'deep watcher; check the watched object size')

    # Dead code: scoped CSS classes that no template class attribute mentions.
    # Only scoped styles are checked; global styles may target other components.
    style_scoped = re.search(r'<style[^>]*\bscoped\b', source)
    if style_scoped and style:
        used = set(re.findall(r'[\w-]+', ' '.join(re.findall(r'class="([^"]*)"', template))))
        # Dynamic :class bindings list classes as quoted strings or object keys
        for binding in re.findall(r':class="([^"]*)"', template):
            used.update(re.findall(r"'([\w-]+)'", binding))
            used.update(re.findall(r'([\w-]+)\s*:', binding))
            used.update(re.findall(r'`([\w-]+)', binding))
        selectors = set()
        for m in CLASS_SELECTOR_RE.finditer(re.sub(r'\{[^}]*\}', '{}', style)):
            selectors.add(m.group(1))
        # Classes built at runtime ('badge-' + x) can't be resolved statically
        prefixes = re.findall(r"'([\w-]+-)'\s*\+|`([\w-]+-)\$\{", template + script)
        prefixes = {a or b for a, b in prefixes}
        # <Transition name="x"> applies x-enter-active etc. at runtime
        transition = re.compile(r'-(enter|leave)(-active|-from|-to)?$|^v-(enter|leave)')
        unused = sorted(c for c in selectors - used
                        if c not in script and not transition.search(c)
                        and not any(c.startswith(p) for p in prefixes))
        if len(unused) >= 3:
            signal('unused-scoped-css', 1, f"{len(unused)} scoped classes never used in template: "
                   + ', '.join(unused[:12]) + (' ...' if len(unused) > 12 else ''))

    # Dead code: names returned from setup() that the template never references
    ret = RETURN_BLOCK_RE.search(script)
    if ret and template:
        names = [n.strip().split(':')[0].strip() for n in ret.group(1).split(',')]
        unused = [n for n in names if n and re.match(r'^\w+$', n) and not re.search(rf'\b{n}\b', template)]
        if unused:
            signal('unused-setup-return', s_start + 1,
                   f"returned from setup() but never used in template: {', '.join(unused)}")

    # Components used in the template but not imported or registered render as
    # unknown elements and fail silently in production builds
    imported = {m.group(1) for m in re.finditer(r'^import\s+(\w+)\s+from', script, re.M)}
    for tag in sorted(set(PASCAL_TAG_RE.findall(template)) - imported - BUILTIN_TAGS):
        if not re.search(rf'\b{tag}\b', script):
            line = line_of(template, template.find('<' + tag), t_start)
            signal('unregistered-component', line, f"<{tag}> is used in the template but never imported or registered")

    for m in STATIC_IMPORT_RE.finditer(script):
        name = m.group(1)
        if re.search(r'(Modal|Dialog|Drawer|Chart)$', name):
            signal('eager-heavy-import', line_of(script, m.start(), s_start),
                   f"{name} imported eagerly; consider defineAsyncComponent if it is conditionally shown")

    return info


def scan_router(root):
    signals = []
    for name in ('main.js', 'main.ts', 'router.js', 'router/index.js', 'router/index.ts'):
        path = root / name
        if not path.exists():
            continue
        text = path.read_text(encoding='utf-8')
        eager = [m.group(1) for m in STATIC_IMPORT_RE.finditer(text) if '/views/' in m.group(2)]
        routed = [n for n in eager if re.search(rf'component:\s*{n}\b', text)]
        if len(routed) > 1:
            signals.append({'file': name, 'kind': 'eager-routes', 'line': 1,
                            'detail': f"{len(routed)} route views imported eagerly: {', '.join(routed)}"})
    return signals


def main():
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    if not args:
        sys.exit(__doc__)
    root = Path(args[0]).resolve()
    files = sorted(p for p in root.rglob('*.vue') if 'node_modules' not in p.parts)
    results = [scan_file(p, root) for p in files]

    # Same helper name defined in several components -> extraction candidate
    owners = defaultdict(list)
    for r in results:
        for fn in r['functions']:
            owners[fn].append(r['file'])
    generic = {'loadData', 'close', 'open', 'toggle', 'handleClick', 'onSubmit'}
    duplicates = {fn: files for fn, files in owners.items() if len(files) > 1 and fn not in generic}

    report = {
        'root': str(root),
        'components': len(results),
        'total_lines': sum(r['lines'] for r in results),
        'files': results,
        'duplicated_helpers': dict(sorted(duplicates.items(), key=lambda kv: -len(kv[1]))),
        'router': scan_router(root),
    }

    if '--json' in sys.argv:
        print(json.dumps(report, indent=2))
        return

    print(f"# Vue scan: {report['components']} components, {report['total_lines']} lines\n")
    print("## Components by size\n")
    print("| File | Lines | Template | Script | Style | Signals |")
    print("|---|---:|---:|---:|---:|---:|")
    for r in sorted(results, key=lambda r: -r['lines']):
        print(f"| {r['file']} | {r['lines']} | {r['template_lines']} | {r['script_lines']} | "
              f"{r['style_lines']} | {len(r['signals'])} |")
    print("\n## Signals (verify each before reporting)\n")
    for r in results:
        for s in r['signals']:
            print(f"- `{r['file']}:{s['line']}` **{s['kind']}**: {s['detail']}")
    for s in report['router']:
        print(f"- `{s['file']}:{s['line']}` **{s['kind']}**: {s['detail']}")
    print("\n## Helpers defined in more than one component\n")
    if not duplicates:
        print("_None found._")
    for fn, where in report['duplicated_helpers'].items():
        print(f"- `{fn}` in {len(where)} files: {', '.join(where)}")


if __name__ == '__main__':
    main()
