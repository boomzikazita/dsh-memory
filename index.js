/**
 * dsh-memory — DSH 专属记忆插件（HMS 架构 → DSH 实现）
 *
 * v0.2.1（2026-10-15，修 6 处运行期缺陷）：
 *   1. 转录钩子 `ctx.on('turn/end')` 事件名不存在（turn/end 只是会话事件类型）⇒ L2 从未运行
 *      → 改 `session/event`（emit，(session,event)）+ 非阻塞 spawn + 每 10 分钟节流
 *   2. primer/guard 注入因 @deepseek-ai/dsh-llm 不可解析静默失效 → 等价兜底工厂 + 失败回队 + 留痕
 *   3. memory_zero_hit 的 L1 切分口径与 rrf_recall 不一致（对**所有**条目恒报零命中）→ split("§") 对齐
 *   4. resource 的 `mem://person/<x>` 未走 safe_resolve（实测可穿越读 vault 外任意 *.md）→ 收敛在 entities 内
 *   5. rrf_recall 预算耗尽把命中截成 **1 个字符**（输出垃圾 "["）→ 停止展开 + truncated 计数
 *   6. guard：EXEMPT_PATH 只判路径字段（原对整段正文匹配＝漏拦）、WRITE_TOOLS 补 memory_conflict、
 *      拦截决策改合法的 {kind:'deny'}（原 'block' 不在 PreToolDecision 联合类型内）
 *
 * v0.2.0（2026-10-14，dsh 0.2 适配 + resource 并入）：
 *   1. 删除 `export const Config = null`（Config 不导出才安全，null 会崩设置页）
 *   2. 并入 resource 工具（自 dsh-resource 迁入，工具名不变）：scripts/resource.py 同包携带，
 *      resource.py 内对 memory scripts 目录的跨插件探测改为同包相对路径
 *   3. Python 子进程：解释器读 config.python（默认 python3），env 补 PYTHONIOENCODING=utf-8 /
 *      PYTHONUTF8=1；SCRIPTS_DIR 改 fileURLToPath(import.meta.url)（铁律 F）
 *   4. agent/pre-step 注入 source 改 v4 形态 { kind: 'plugin:dsh-memory' }；
 *      guard 钩子执行参数读 exec.arguments ?? exec.args
 *
 * 分层：L1 规则(MEMORY/USER) / L2 会话 FTS5 / L3 语义(可选 MCP) / L4 wiki 活指针
 *       + RRF 四路融合 + 确认制写入 + Fernet 加密库 + 维护自动化工具
 *
 * 结构（对应 dsh-cybernetics 模板）：
 *   工具   memory_recall / memory_suggest / memory_confirm / memory_reject
 *          memory_vault_get / memory_vault_set / memory_harvest / memory_health / memory_backup
 *   钩子   primer(agent/pre-step 门控注入) / harvest(turn/end 转录) / guard(tools/pre-execute 敏感扫描)
 * 实现：host 侧执行，Python 脚本承载算法（rrf_recall.py / vault.py / harvest.py）
 */
import { mkdirSync, existsSync, appendFileSync, readFileSync, writeFileSync, renameSync, rmSync, statSync, readdirSync } from 'node:fs'
import { createHash, randomUUID } from 'node:crypto'
import { execFileSync, spawn } from 'node:child_process'
import { resolve, join, dirname } from 'node:path'
import { homedir } from 'node:os'
import { fileURLToPath } from 'node:url'
import { defineTool } from '@deepseek-ai/dsh-tools'

export const name = 'dsh-memory'
export const inject = ['tools', 'agents']

const HOME = homedir()
const DEFAULT_MEMORY_DIR = join(HOME, '.dsh', 'memories')
// 铁律 F：fileURLToPath 解析插件自身路径（new URL().pathname 裸取在 Windows 产非法路径）
const SCRIPTS_DIR = join(dirname(fileURLToPath(import.meta.url)), 'scripts')

// 敏感模式（写前扫描；命中 → 警告/拦截/重定向 vault）
const SENSITIVE_PATTERNS = [
  { id: 'api-key', re: /\b(sk|pk|rk)-[A-Za-z0-9]{16,}\b/i, label: 'API Key' },
  { id: 'aws-key', re: /\bAKIA[0-9A-Z]{16}\b/, label: 'AWS Access Key' },
  { id: 'secret-assign', re: /\b(api[_-]?key|token|secret|password|passwd)\s*[:=]\s*["']?[A-Za-z0-9_\-.]{8,}/i, label: '密钥/口令赋值' },
  { id: 'phone', re: /\b1[3-9]\d{9}\b/, label: '手机号' },
  { id: 'id-card', re: /\b\d{17}[\dXx]\b/, label: '身份证号' },
  { id: 'cookie', re: /\bcookie\s*[:=]/i, label: 'Cookie' },
]
// 豁免路径（凭据类，允许明文）
const EXEMPT_PATH = /(\.env|credentials|\.vault-key|VAULT\.md\.enc|\.git\/)/i
// 写类工具（guard 扫描对象；bash 太噪，不扫）
// v0.2.1：补 memory_conflict（merge/replace 会把 agent 原文写进 L1，原集合漏扫）
const WRITE_TOOLS = new Set(['write', 'edit', 'memory_suggest', 'memory_vault_set', 'memory_conflict'])
// primer 门控：实体/主题关键词（命中才召回注入；默认零注入）
const PRIMER_GATE = [
  '记忆', '记住', '忘了', '偏好', '人物', '档案', '端口', '配置', '会议纪要',
  'A股', '控制论', 'Honcho', 'ZeroTier', '飞书', 'Vaultwarden', '老郑', '句号',
]

/**
 * 注入 user 消息的稳健工厂（v0.2.1）
 *
 * 优先用宿主 `@deepseek-ai/dsh-llm` 的 createUserMessage。但该裸导入在插件**真实目录**下
 * 可能解析不到（本机实测 ERR_MODULE_NOT_FOUND：/home/boom/dsh-plugins/node_modules/@deepseek-ai/
 * 只有 dsh-tools；Node 按 realpath 向上查找，profile 那层的 dsh-llm 用不到），
 * 原实现把它放在 try 里静默吞掉 ⇒ primer/guard 整条注入链路恒失效。
 * 故：解析失败时按宿主 createMessage 的等价形态兜底（role/content/source/id + 冻结），
 * 保证注入不因依赖缺失而整条死掉；同时一次性 console.warn 留痕。
 */
let _createUserMessage = null // null=未探测 / function=宿主工厂 / false=不可用
let _injectFactoryWarned = false
async function makeInjectedUserMessage({ text, kind }) {
  const content = [{ type: 'text', text }]
  if (_createUserMessage === null) {
    try {
      const mod = await import('@deepseek-ai/dsh-llm')
      _createUserMessage = typeof mod?.createUserMessage === 'function' ? mod.createUserMessage : false
    } catch {
      _createUserMessage = false
    }
  }
  if (typeof _createUserMessage === 'function') {
    return _createUserMessage({ content, source: { kind } })
  }
  if (!_injectFactoryWarned) {
    _injectFactoryWarned = true
    console.warn('[dsh-memory] @deepseek-ai/dsh-llm 不可解析，注入消息走等价兜底形态（建议补 node_modules/@deepseek-ai/dsh-llm 软链）')
  }
  return Object.freeze({ id: randomUUID(), role: 'user', content, source: { kind } })
}

/** 从消息 content（字符串或块数组）提取纯文本 */
function extractText(content) {
  if (typeof content === 'string') return content
  if (Array.isArray(content)) {
    return content.filter((b) => b && b.type === 'text' && typeof b.text === 'string').map((b) => b.text).join(' ')
  }
  return ''
}

/**
 * trigger 保障（2026-08-19）：保证写入 L1 的每条记忆都带 `<!-- trigger: ... -->`。
 * 若内容已含 trigger 则原样返回；否则从 #标签 + 中文高频词（2-6 字，去停用词）自动提炼，
 * 附加在内容末尾。design 依据：MEMORY.md §4.2 格式要求每条带 trigger（触发词检索）。
 */
const TRIGGER_STOP = new Set(['一个','我们','他们','这个','那个','什么','怎么','需要','要求','进行','以及','相关','问题','时候','内容','这些','那些','必须','可以','不要','不能','应该','用户','系统','直接','自动','然后','并且','或者','如果','因为','所以','但是','就是','没有','已经','还是','可能','主要','所有','其他','一般','具体','这里','那里','之后','之前','现在','今天','昨天','明天','最后','第一','第二','第三','方面','情况','方式','方法','过程','结果','影响','作用','目的','意义','来源','涉及','包含','包括','属于','存在','出现','发生','发现','导致','造成','通过','使用','利用','采用','进行'])
const TRIGGER_META_TAGS = new Set(['规则','偏好','边界','记忆','用户','工作流','标准','安全','规范','工具','配置','验证','事实','线索','经验','教训'])
function ensureTrigger(text) {
  if (!text) return text
  if (/<!--\s*trigger:/.test(text)) return text
  // ① #标签（排除元标签）
  const tags = [...text.matchAll(/#([\u4e00-\u9fffA-Za-z0-9_-]+)/g)].map((m) => m[1]).filter((t) => !TRIGGER_META_TAGS.has(t))
  // ② 中文词频（2-6 字，去停用词）
  const cjk = text.replace(/[^\u4e00-\u9fff]/g, ' ')
  const freq = {}
  for (const w of cjk.split(/\s+/)) {
    if (w.length < 2 || w.length > 6 || TRIGGER_STOP.has(w)) continue
    freq[w] = (freq[w] || 0) + 1
  }
  const topWords = Object.entries(freq).sort((a, b) => b[1] - a[1]).slice(0, 4).map(([w]) => w)
  // ③ 合并去重，限 6 个
  const trig = [...new Set([...tags, ...topWords])].slice(0, 6)
  if (!trig.length) trig.push('memory')
  return `${text} <!-- trigger: ${trig.join(',')} -->`
}

/** 手术级注入分档（§8.5）：按任务类型与输入长度自动切换 400/1200/2000 */
function classifyBudget(text) {
  const len = (text || '').length
  const deep = /控制论|复盘|策略|评估|设计|方案|深度|人物|情感|多线程|文档审阅|分析/.test(text)
  const simple = /^(查|看|确认|路径|端口|密码|地址|时间|几点)/.test(text) || len <= 24
  if (deep) return 2000
  if (simple) return 400
  return 1200
}

export function apply(ctx, config = {}) {
  function expandHome(p) {
    if (typeof p !== 'string' || p === '') return p
    if (p === '~') return HOME
    if (p.startsWith('~/')) return join(HOME, p.slice(2))
    return p
  }
  const memoryDir = expandHome(config.memoryDir) || DEFAULT_MEMORY_DIR
  const scriptsDir = expandHome(config.scriptsDir) || SCRIPTS_DIR
  const budgets = { MEMORY: Number(config.budgetMemory ?? 4400), USER: Number(config.budgetUser ?? 2750) }
  const guardBlock = config.guardBlock === true
  const primerBudget = Number(config.primerBudget ?? 800)
  const primerTopK = Number(config.primerTopK ?? 3)
  // v0.2.0：Python 解释器走 config.python（默认 python3）；env 强制 UTF-8 防中文输出乱码
  const PYTHON = config.python || 'python3'
  const PY_ENV = { ...process.env, PYTHONIOENCODING: 'utf-8', PYTHONUTF8: '1' }

  const state = { pendingWarns: [] }

  // ---------- 工具函数 ----------
  const py = (script, args, input) => {
    try {
      const out = execFileSync(PYTHON, [join(scriptsDir, script), ...args], {
        input: input ?? '', encoding: 'utf8', timeout: 30000, maxBuffer: 16 * 1024 * 1024, env: PY_ENV,
      })
      return { ok: true, out: out.trim() }
    } catch (e) {
      return { ok: false, out: String(e?.stderr || e?.message || e).trim() }
    }
  }
  const file = (name) => join(memoryDir, name)
  const read = (name) => (existsSync(file(name)) ? readFileSync(file(name), 'utf8') : '')
  const append = (name, text) => { mkdirSync(memoryDir, { recursive: true }); appendFileSync(file(name), text) }
  const charCount = (name) => read(name).length
  const budgetFor = (track) => (track === 'user' ? budgets.USER : budgets.MEMORY)
  const targetFor = (track) => (track === 'user' ? 'USER.md' : 'MEMORY.md')

  // ---------- 同主题重叠检测（2026-08-22，借鉴论文 Rethinking Memory 4.2.3 选择性写入） ----------
  // 目标：memory_suggest 提交时提示与既有 L1 条目 / pending 提案的重叠，推动合并而非盲追加。
  // 度量：bigram 字符重叠率（中文核心）+ #标签交集；≥阈值判"重叠候选"。
  function bigramsOf(text) {
    const s = text.replace(/#[\u4e00-\u9fffA-Za-z0-9_-]+/g, ' ').replace(/<!--.*?-->/g, ' ')
    const grams = new Set()
    for (let i = 0; i < s.length - 1; i++) {
      const g = s.slice(i, i + 2)
      if (/[\u4e00-\u9fff]/.test(g) || /[A-Za-z0-9]/.test(g[0]) && /[A-Za-z0-9]/.test(g[1])) grams.add(g)
    }
    return grams
  }
  function overlapOf(a, b) {
    const ga = bigramsOf(a), gb = bigramsOf(b)
    if (!ga.size || !gb.size) return 0
    let hit = 0
    for (const g of ga) if (gb.has(g)) hit++
    return hit / Math.min(ga.size, gb.size)
  }
  function tagsOf(text) {
    return new Set([...text.matchAll(/#([\u4e00-\u9fffA-Za-z0-9_-]+)/g)].map((m) => m[1]))
  }
  /** 返回与 content 重叠的既有条目/提案：{kind, head, overlap, tags} 列表（按重叠度降序，≤3） */
  function findOverlaps(content) {
    const out = []
    const cTags = tagsOf(content)
    // ① 既有 L1（MEMORY.md / USER.md，§ 分隔）
    for (const name of ['MEMORY.md', 'USER.md']) {
      const raw = read(name)
      if (!raw.trim()) continue
      for (const ent of raw.split('§')) {
        const e = ent.trim()
        if (!e || e.length < 10) continue
        const ov = overlapOf(content, e)
        const eTags = tagsOf(e)
        let tagHit = 0
        for (const t of cTags) if (eTags.has(t)) tagHit++
        if (ov >= 0.35 || (ov >= 0.2 && tagHit >= 1)) {
          out.push({ kind: `L1:${name}`, head: e.slice(0, 60), overlap: Math.round(ov * 100), tags: tagHit })
        }
      }
    }
    // ② pending 提案（同 track 未确认）
    for (const s of loadSuggestions()) {
      if (s.status !== 'pending') continue
      const ov = overlapOf(content, s.content)
      if (ov >= 0.35) {
        out.push({ kind: 'pending', head: s.content.slice(0, 60), overlap: Math.round(ov * 100), tags: 0 })
      }
    }
    out.sort((a, b) => b.overlap - a.overlap || b.tags - a.tags)
    return out.slice(0, 3)
  }

  function loadSuggestions() {
    const raw = read('SUGGESTIONS.jsonl')
    if (!raw.trim()) return []
    return raw.split('\n').filter(Boolean).map((l) => { try { return JSON.parse(l) } catch { return null } }).filter(Boolean)
  }
  function saveSuggestions(list) {
    writeFileSync(file('SUGGESTIONS.jsonl'), list.map((s) => JSON.stringify(s)).join('\n') + (list.length ? '\n' : ''))
  }

  // ---------- 1. 工具：memory_recall ----------
  ctx.tools.register(defineTool({
    name: 'memory_recall',
    description: '五路 RRF 记忆召回（L1 规则/L2 会话/L3 语义/L4 wiki/L5 时间窗），返回带来源标记的证据账本。时间性查询（"昨天聊了什么"）务必带 date 走 L5；deep=true 深度召回（topk=8/预算2000）用于多跳/结论依赖细节。',
    parameters: {
      query: { type: 'string', required: true, description: '召回查询（自然语言/关键词/实体名）' },
      budget: { type: 'number', description: '输出字符预算，默认 800（手术级注入档位：简单 400 / 常规 1200 / 深度 2000）' },
      topk: { type: 'number', description: '返回条数，默认 5' },
      deep: { type: 'boolean', description: '深度召回（通道2 升级），默认 false' },
      date: { type: 'string', description: 'L5 时间窗召回：YYYY-MM-DD / today / yesterday / -N（N 天前）' },
    },
    output: {
      schema: { type: 'string', description: '证据账本文本' },
      render: (args, value) => [{ type: 'text', text: value }],
    },
    async execute(input) {
      const budget = input?.budget ?? 800
      const topk = input?.topk ?? 5
      const deep = input?.deep === true
      const args = [input.query, '--budget', String(budget), '--topk', String(topk)]
      if (deep) args.push('--deep')
      if (input?.date) args.push('--date', String(input.date))
      const r = py('rrf_recall.py', args)
      if (!r.ok) return `[memory_recall] 失败：${r.out}`
      return r.out
    },
  }))

  // ---------- 1.5 工具：memory_graph（关系图谱沿边查询，resource 方案沿边数据源） ----------
  ctx.tools.register(defineTool({
    name: 'memory_graph',
    description: '关系图谱沿边查询（curated 类型边 + 自动共现边）。输入实体名或含实体的自然语言，返回邻接关系；用于"谁和谁什么关系/参与过什么"类查询。',
    parameters: {
      query: { type: 'string', required: true, description: '实体名或含实体的自然语言（如 句号 / 老郑参与过哪些项目）' },
      min_w: { type: 'number', description: '自动共现边权重下限，默认 0（过滤弱边噪音用，如 4）' },
    },
    output: {
      schema: { type: 'string', description: '沿边证据文本' },
      render: (args, value) => [{ type: 'text', text: value }],
    },
    async execute(input) {
      const args = [input.query]
      if (input?.min_w) args.push('--min-w', String(input.min_w))
      const r = py('graph_query.py', args)
      if (!r.ok) return `[memory_graph] 失败：${r.out}`
      return r.out
    },
  }))

  // ---------- 2. 工具：memory_suggest（确认制写入提案） ----------
  ctx.tools.register(defineTool({
    name: 'memory_suggest',
    description: '提交记忆写入提案（进待确认队列，不直接写）。track: memory=全局规则/USER=用户档案/skill=技能草案。lifecycle: rule=常驻/stable=稳定/situational=情境（自动带 expires 默认90天）/record=一次性记录（不进 L1 归 wiki）。敏感内容用 memory_vault_set',
    parameters: {
      content: { type: 'string', required: true, description: '提案内容（§ 格式条目或技能草案）' },
      track: { type: 'string', description: 'memory / user / skill，默认 memory' },
      lifecycle: { type: 'string', description: 'rule / stable / situational / record，默认 rule' },
      expiresDays: { type: 'number', description: '情境类有效期天数（默认 90，仅 lifecycle=situational 时生效）' },
    },
    output: {
      schema: { type: 'string', description: '提案状态' },
      render: (args, value) => [{ type: 'text', text: value }],
    },
    async execute(input) {
      const track = input?.track ?? 'memory'
      if (!['memory', 'user', 'skill'].includes(track)) return `[memory_suggest] track 必须为 memory/user/skill`
      // 生命周期分类（2026-08-20 治理规则 MEMORY.md 首条：放什么/放多久）
      const lifecycle = input?.lifecycle ?? 'rule'
      if (!['rule', 'stable', 'situational', 'record'].includes(lifecycle)) return `[memory_suggest] lifecycle 必须为 rule/stable/situational/record`
      let expires = null
      if (lifecycle === 'situational') {
        const days = Number(input?.expiresDays) || 90
        if (days < 30 || days > 365) return `[memory_suggest] expiresDays 应在 30-365（情境类生命周期）`
        expires = new Date(Date.now() + days * 86400000).toISOString().slice(0, 10)
      }
      // trigger 保障（2026-08-19）：无 trigger 的提案自动提炼补充，确保落盘条目必有触发词
      const content = ensureTrigger(input.content ?? '')
      // 敏感扫描：命中则拒绝并引导 vault
      for (const p of SENSITIVE_PATTERNS) {
        if (p.re.test(content)) {
          return `[memory_suggest] 内容含敏感模式（${p.label}），禁止写入明文记忆。请改用 memory_vault_set 存加密库，明文只留 → vault:<ref> 引用。`
        }
      }
      // Persona 约束（2026-08-23，借鉴 TencentDB-Agent-Memory persona-generation）：
      // USER.md 是用户画像，遵守四条：长度上限、禁止过度推测、只从对话数据来、冷启动克制。
      if (track === 'user') {
        if (content.length > 2000) {
          return `[memory_suggest] USER.md persona 超长（${content.length} 字符 > 2000）。请精简为画像级表述，细节归 wiki/vault。`
        }
        if (/(推测|可能是个|应该是|估计(?:是|他|她))/.test(content)) {
          return `[memory_suggest] persona 含推测性表述（过度推测禁止）：画像只能来自对话中明确说出的信息，宁缺毋滥。请改写为已确认事实。`
        }
      }
      // 预算检查
      const budget = budgetFor(track)
      const current = charCount(targetFor(track))
      const extra = content.length
      if (current + extra > budget) {
        return `[memory_suggest] 预算超限：${targetFor(track)} 当前 ${current}/${budget}，新增 ${extra} 会超出。请先合并/归档（见 memory_health）。`
      }
      const suggestions = loadSuggestions()
      const item = {
        id: `s-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`,
        ts: new Date().toISOString(), track, content, status: 'pending', lifecycle,
      }
      if (expires) item.expires = expires
      // 同主题重叠检测（2026-08-22）：提示与既有 L1 / pending 提案的合并候选，抑制记忆冗余。
      // 2026-10-14 修正：必须在入队前计算——否则 loadSuggestions() 会把刚 push 的提案自己
      // 判成 100% 重叠（每次提交都误报"与既有记忆重叠 pending[100%]"）。
      const overlaps = findOverlaps(content)
      suggestions.push(item)
      saveSuggestions(suggestions)
      let warn = ''
      if (overlaps.length) {
        const parts = overlaps.map((o) => `${o.kind}[${o.overlap}%${o.tags ? `,标签${o.tags}` : ''}]「${o.head}…」`)
        warn = ` ⚠️ 与既有记忆重叠：${parts.join('；')}。建议采纳时合并/更新既有条目而非新增（可调 memory_conflict detect 看批量候选、merge 执行合并；L1 预算 ${current + extra}/${budget}）。`
      }
      if (lifecycle === 'record') warn += '（提示：record=完成记录，按治理规则应归 wiki 而非 L1，weekly-review 会提归档）'
      if (lifecycle === 'situational') warn += `（情境类，expires=${expires}，到期 weekly-review 提归档）`
      // 2026-10-14：条数口径与 memory_health 对齐——只数 pending（原先数全量行，含已 rejected，显得像积压）
      const pendingCount = suggestions.filter((s) => s.status === 'pending').length
      return `[memory_suggest] 提案 ${item.id} 已入队（track=${track} lifecycle=${lifecycle}${expires ? ` expires=${expires}` : ''}，待确认队列共 ${pendingCount} 条）${warn}。待 memory_confirm 采纳。`
    },
  }))

  // ---------- 3. 工具：memory_confirm / memory_reject ----------
  async function applyConfirmed(item) {
    if (item.track === 'skill') {
      const slug = (item.content.match(/name:\s*([a-z0-9-]+)/i)?.[1]) || `skill-${Date.now()}`
      const dir = join(HOME, '.dsh', 'skills', slug)
      const skillFile = join(dir, 'SKILL.md')
      // 内容 hash 幂等（2026-08-23，借鉴 TencentDB-Agent-Memory skill-versioning）：
      // 与已落盘 SKILL.md 内容相同则拒绝，防重复蒸馏/重复提交
      if (existsSync(skillFile)) {
        const prev = readFileSync(skillFile, 'utf8')
        if (createHash('md5').update(prev).digest('hex') === createHash('md5').update(item.content).digest('hex')) {
          return `内容与 ~/.dsh/skills/${slug}/SKILL.md 完全相同（md5 一致），已存在，拒绝重复落盘`
        }
      }
      mkdirSync(dir, { recursive: true })
      writeFileSync(skillFile, item.content)
      return `已写入 ~/.dsh/skills/${slug}/SKILL.md`
    }
    // trigger 兜底（2026-08-19）：历史提案可能无 trigger，落盘前补齐
    const content = ensureTrigger(item.content)
    const target = targetFor(item.track)
    const prefix = item.track === 'user' ? 'USER.md' : 'MEMORY.md'
    // 写入安全（§4.6）：读-改-原子写 + pre-image 记录；冲突（文件被并发改动）则放弃本轮
    const preImage = read(target)
    const hashPre = createHash('sha256').update(preImage).digest('hex').slice(0, 12)
    const newContent = `${preImage.trim() ? preImage.trimEnd() + '\n§\n' : ''}${content.trim()}\n`
    const tmpPath = file(`${target}.tmp`)
    writeFileSync(tmpPath, newContent)
    const postImage = read(target)
    if (postImage !== preImage) {
      try { rmSync(tmpPath) } catch { /* 清理临时文件 */ }
      return `并发冲突：${target} 已被外部修改，本轮采纳已放弃（请重试）`
    }
    renameSync(tmpPath, file(target))
    append('DREAMS.md', `- ${new Date().toISOString()} 采纳 ${item.id} (${item.track}) → ${prefix} | pre=${hashPre} | +${item.content.length}ch\n`)
    return `已写入 ${target}`
  }
  ctx.tools.register(defineTool({
    name: 'memory_confirm',
    description: '采纳一条待确认记忆提案：memory/user 追加进 MEMORY.md/USER.md（§ 分隔），skill 落盘 ~/.dsh/skills/<name>/SKILL.md',
    parameters: { id: { type: 'string', required: true, description: '提案 id（memory_suggest 返回）' } },
    output: { schema: { type: 'string', description: '结果' }, render: (args, value) => [{ type: 'text', text: value }] },
    async execute(input) {
      const list = loadSuggestions()
      const item = list.find((s) => s.id === input.id && s.status === 'pending')
      if (!item) return `[memory_confirm] 未找到待确认提案 ${input.id}`
      const result = await applyConfirmed(item)
      item.status = 'confirmed'
      item.confirmedAt = new Date().toISOString()  // 增益验证门（EDGE 启发）：记录采纳时间，供零命中检测
      saveSuggestions(list)
      return `[memory_confirm] ${result}`
    },
  }))
  ctx.tools.register(defineTool({
    name: 'memory_reject',
    description: '拒绝一条待确认记忆提案（从队列移除）',
    parameters: { id: { type: 'string', required: true, description: '提案 id' } },
    output: { schema: { type: 'string', description: '结果' }, render: (args, value) => [{ type: 'text', text: value }] },
    async execute(input) {
      const list = loadSuggestions()
      const item = list.find((s) => s.id === input.id && s.status === 'pending')
      if (!item) return `[memory_reject] 未找到待确认提案 ${input.id}`
      item.status = 'rejected'
      saveSuggestions(list)
      return `[memory_reject] 已拒绝 ${input.id}`
    },
  }))

  // ---------- 2.5 工具：memory_conflict（L1 批量冲突检测 + 合并执行，2026-08-23） ----------
  // 借鉴 TencentDB-Agent-Memory l1-dedup：批量比较新记忆 vs 候选池，产出
  // store/skip/update/merge 动作；机械层由 conflict_detect.py 完成，语义兜底由 agent
  // 决策，合并落盘走 conflict_merge.py（时间戳并集 + pre-image 审计）。
  ctx.tools.register(defineTool({
    name: 'memory_conflict',
    description: 'L1 批量冲突检测与合并执行：①检测=扫描 pending 提案 vs 既有 MEMORY.md/USER.md 条目，输出 store/skip/update/merge 候选（agent 复核语义后执行）；②合并=把多条相关记忆合并为一条（删除目标条目+写入合并内容+时间戳并集+记 DREAMS.md）。合并/采纳前调用可抑制记忆冗余。',
    parameters: {
      action: { type: 'string', required: true, description: 'detect=批量冲突检测（默认） / merge=执行合并 / replace=单条目原位更新' },
      track: { type: 'string', description: 'memory / user，默认 memory' },
      content: { type: 'string', description: 'merge/replace 时的合并后内容（merge 必填）' },
      targetIds: { type: 'array', items: { type: 'string' }, description: '目标条目：L1 索引（0-based，detect 输出里的 target_id）或内容子串；merge 可多个（多对多合并），replace 一个' },
      dryRun: { type: 'boolean', description: '只显示将执行的变更，不落盘（默认 false）' },
    },
    output: { schema: { type: 'string', description: '检测结果或合并结果' }, render: (args, value) => [{ type: 'text', text: value }] },
    async execute(input) {
      const action = input?.action ?? 'detect'
      const track = input?.track ?? 'memory'
      if (!['memory', 'user'].includes(track)) return `[memory_conflict] track 必须为 memory/user`
      if (action === 'detect') {
        const r = py('conflict_detect.py', ['--track', track])
        return r.ok ? r.out : `[memory_conflict] 检测失败：${r.out}`
      }
      if (action === 'merge' || action === 'replace') {
        const content = input?.content ?? ''
        const ids = Array.isArray(input?.targetIds) ? input.targetIds.map(String) : (input?.targetIds ? [String(input.targetIds)] : [])
        if (!ids.length) return `[memory_conflict] ${action} 需要 targetIds`
        if (action === 'merge' && !content.trim()) return `[memory_conflict] merge 需要 content（合并后内容）`
        const args = [action, track, ...(action === 'merge' ? [content, ...ids] : [ids[0], content])]
        if (input?.dryRun) args.push('--dry-run')
        const r = py('conflict_merge.py', args)
        return r.ok ? r.out : `[memory_conflict] ${action} 失败：${r.out}`
      }
      return `[memory_conflict] action 必须为 detect/merge/replace`
    },
  }))

  // ---------- 3.5 工具：memory_zero_hit（增益验证门，EDGE 启发，2026-08-25） ----------
  // 借鉴 EDGE（arXiv:2608.21946）：不是所有记忆都值得长期保留，只有被验证带来
  // 正增益（被召回/被用到）的才值得留在 L1。本工具不拦截写入，只做观测：
  // 扫描 MEMORY.md/USER.md 条目，对照 .usage.json 的召回统计，报告零命中/陈旧条目。
  ctx.tools.register(defineTool({
    name: 'memory_zero_hit',
    description: '增益验证门（EDGE 启发）：扫描 L1 条目，对照召回统计（.usage.json），报告"已采纳但从未被召回/长期未召回"的零增益条目，供 weekly-review 或人工淘汰。只读不改。',
    parameters: { days: { type: 'number', description: '陈旧阈值天数，默认 30（N 天未召回视为陈旧）' } },
    output: { schema: { type: 'string', description: '零增益条目报告' }, render: (args, value) => [{ type: 'text', text: value }] },
    async execute(input) {
      const days = Number(input?.days) || 30
      const r = py('memory_zero_hit.py', ['--days', String(days)])
      return r.ok ? r.out : `[memory_zero_hit] 失败：${r.out}`
    },
  }))

  // ---------- 4. 工具：memory_vault_get / memory_vault_set（Fernet 加密库） ----------
  ctx.tools.register(defineTool({
    name: 'memory_vault_get',
    description: '从加密库读取敏感条目（如 vault:person/句号）。结果标 SENSITIVE：只用于当前任务，禁止写入会话日志或明文记忆',
    parameters: { ref: { type: 'string', required: true, description: 'vault 引用（vault:xxx 或 xxx）' } },
    output: { schema: { type: 'string', description: '条目内容' }, render: (args, value) => [{ type: 'text', text: value }] },
    async execute(input) {
      const ref = input.ref.replace(/^vault:/, '')
      const r = py('vault.py', ['get', ref])
      if (!r.ok) return `[memory_vault_get] ${r.out}`
      return `[SENSITIVE] ${r.out}`
    },
  }))
  ctx.tools.register(defineTool({
    name: 'memory_vault_set',
    description: '写入加密库条目（敏感内容专用）。写入后明文只允许出现 → vault:<ref> 引用，禁止出现原文',
    parameters: {
      ref: { type: 'string', required: true, description: '条目引用（如 person/句号、credential/vaultwarden）' },
      value: { type: 'string', required: true, description: '条目内容（敏感原文）' },
    },
    output: { schema: { type: 'string', description: '结果' }, render: (args, value) => [{ type: 'text', text: value }] },
    async execute(input) {
      const ref = input.ref.replace(/^vault:/, '')
      const r = py('vault.py', ['set', ref, input.value])
      return r.ok ? `[memory_vault_set] ${r.out}` : `[memory_vault_set] 失败：${r.out}`
    },
  }))

  // ---------- 5. 工具：memory_harvest / memory_health / memory_backup ----------
  ctx.tools.register(defineTool({
    name: 'memory_harvest',
    description: '转录 DSH 会话日志到 L2 FTS5 索引并提取纠正信号（供 skill 蒸馏）。自动化维护用',
    parameters: {},
    output: { schema: { type: 'string', description: '结果' }, render: (args, value) => [{ type: 'text', text: value }] },
    async execute() {
      const idx = py('harvest.py', ['index'])
      const sig = py('harvest.py', ['signals'])
      return `${idx.out}\n${sig.out}`
    },
  }))
  ctx.tools.register(defineTool({
    name: 'memory_health',
    description: '记忆系统健康检查：L1 预算占用、文件完整性、vault 可解密、L2 索引、git 状态。返回各层状态一行',
    parameters: {},
    output: { schema: { type: 'string', description: '健康报告' }, render: (args, value) => [{ type: 'text', text: value }] },
    async execute() {
      const lines = []
      for (const [track, f] of [['MEMORY', 'MEMORY.md'], ['USER', 'USER.md']]) {
        const c = charCount(f)
        const b = budgets[track]
        lines.push(`${f}: ${c}/${b} (${Math.round((c / b) * 100)}%)${c > b ? ' ⚠超预算' : ''}`)
      }
      lines.push(`vault: ${py('vault.py', ['check']).ok ? 'OK' : '异常'}`)
      lines.push(`L2 索引: ${existsSync(file('index.sqlite')) ? '存在' : '未建立（跑 memory_harvest）'}`)
      const sug = loadSuggestions().filter((s) => s.status === 'pending')
      lines.push(`待确认队列: ${sug.length} 条`)
      lines.push(`SUGGESTIONS 违规扫描: ${sug.filter((s) => SENSITIVE_PATTERNS.some((p) => p.re.test(s.content))).length} 条含敏感模式`)
      // L6 图谱与实体表检查（2026-08-19 新增）
      const graphFile = join(HOME, '.dsh', 'data', 'relations.json')
      if (existsSync(graphFile)) {
        const ageDays = (Date.now() - statSync(graphFile).mtimeMs) / 86400000
        lines.push(`L6 图谱: ${ageDays < 1 ? '今日更新' : Math.round(ageDays) + ' 天前更新'}${ageDays > 7 ? ' ⚠过期(>7天，跑 memory-graph-refresh)' : ''}`)
      } else {
        lines.push('L6 图谱: 未生成（跑 python3 ~/.dsh/scripts/light_graph.py）')
      }
      try {
        const rrfSrc = readFileSync(join(scriptsDir, 'rrf_recall.py'), 'utf8')
        // 实体表对账（2026-08-20 修正）：数量对比无意义（含项目/主题/别名），
        // 改为"人物差集"：_KNOWN_PERSONS 别名归一后，每个规范人物应有 entities/ 页
        const alias = { sophia: '藏妤', 郑鑫燚: '老郑', 郑铭燚: '老郑' } // 与 light_graph.py PERSON_ALIASES 对齐
        const pBlock = rrfSrc.match(/_KNOWN_PERSONS = frozenset\(\{([\s\S]*?)\}\)/)
        const persons = [...(pBlock?.[1]?.match(/["'][^"']+["']/g) || [])].map((s) => s.replace(/["']/g, ''))
        const canonical = [...new Set(persons.map((p) => alias[p] ?? p))]
        const entDir = join(HOME, 'Nutstore Files', '我的坚果云', 'wiki', 'entities')
        const entNames = existsSync(entDir) ? readdirSync(entDir).filter((f) => f.endsWith('.md')).map((f) => f.slice(0, -3)) : []
        const missing = canonical.filter((p) => !entNames.includes(p))
        const ok = canonical.length > 0 && missing.length === 0
        lines.push(`实体表对账: 人物 ${canonical.length - missing.length}/${canonical.length} 有实体页${ok ? ' ✓' : `（缺页: ${missing.join('、')}）`}`)
      } catch { /* 对账失败不阻断健康检查 */ }
      return `[memory_health]\n` + lines.join('\n')
    },
  }))
  ctx.tools.register(defineTool({
    name: 'memory_backup',
    description: '记忆 git 备份：~/.dsh/memories 提交并推送远端（ZeroTier）。.vault-key/scratch 忽略，VAULT.md.enc 密文随仓库同步',
    parameters: {},
    output: { schema: { type: 'string', description: '结果' }, render: (args, value) => [{ type: 'text', text: value }] },
    async execute() {
      const git = (args) => {
        try {
          return execFileSync('git', ['-C', memoryDir, ...args], { encoding: 'utf8', timeout: 60000 }).trim()
        } catch (e) { return null }
      }
      if (!existsSync(join(memoryDir, '.git'))) {
        git(['init'])
        writeFileSync(join(memoryDir, '.gitignore'), '.vault-key\nscratch.md\n__pycache__/\n')
        git(['add', '-A'])
        git(['commit', '-m', 'init memory repo'])
      }
      git(['add', '-A'])
      const diff = git(['diff', '--cached', '--name-only'])
      if (!diff) return `[memory_backup] 无变更`
      const commit = git(['commit', '-m', `memory backup ${new Date().toISOString()}`])
      const push = git(['push'])
      return `[memory_backup] ${commit || 'no commit'}\n${push === null ? '（无远端，仅本地提交）' : '远端已推送'}`
    },
  }))

  // ---------- 5a. 工具：check_assets（按需资产查重，2026-08-20 防重复建设） ----------
  ctx.tools.register(defineTool({
    name: 'check_assets',
    description: '按需资产查重：新建脚本/skill/插件/配置/automation 前调用，检索已有相近资产（~/.dsh/scripts、skills、plugins、状态卡变更记录、automations）。传功能关键词，返回命中清单；未命中提示可安全新建。',
    parameters: {
      keywords: { type: 'array', items: { type: 'string' }, required: true, description: '查重关键词（功能名/动作，如 自检、采集、飞书、备份）' },
    },
    output: { schema: { type: 'string', description: '相近资产清单或未命中提示' }, render: (args, value) => [{ type: 'text', text: value }] },
    async execute(input) {
      const kws = Array.isArray(input?.keywords) ? input.keywords : [String(input?.keywords ?? '')]
      const r = py('check_assets.py', kws)
      return r.ok ? r.out : `[check_assets] 查重失败: ${r.out.slice(0, 200)}`
    },
  }))

  // ---------- 5b. 工具：memory_notify（飞书推送） ----------
  ctx.tools.register(defineTool({
    name: 'memory_notify',
    description: '向飞书聊天推送消息（周报/告警/提醒）。需要 LARK_APP_ID/LARK_APP_SECRET（环境变量或 ~/.env）与目标 chat_id；未配置时返回提示不报错',
    parameters: {
      text: { type: 'string', required: true, description: '推送内容' },
      chatId: { type: 'string', description: '目标 chat_id，默认 oc_e6a44a01ec13fe6d94246fbb8df153af（hermes 时代主会话，唯一已验证推送通道）' },
    },
    output: { schema: { type: 'string', description: '推送结果' }, render: (args, value) => [{ type: 'text', text: value }] },
    async execute(input) {
      const r = py('feishu_push.py', [input.text, input.chatId || ''])
      return r.ok ? r.out : `[memory_notify] ${r.out}`
    },
  }))

  // ---------- 5c. 工具：resource（统一资源视图 mem://+wiki:// 只读门面，v0.2.0 自 dsh-resource 并入） ----------
  // 设计（2026-08-22，Mirage"抄心智不抄架构" + codex world-state 思想）：
  //   一个工具三个原语：ls（域里有什么）/ read（读引用）/ search（跨域检索）；
  //   引用格式 <ns>://<path>：路径即身份；只读优先，路径穿越防护与输出上限在 scripts/resource.py。
  //   不吞并专用能力：深度语义召回走 memory_recall；加密库走 memory_vault_get；写操作走原工具。
  // v0.2.0 并入后：resource.py 与本包 scripts 同目录（同包相对路径，不再跨插件探测），
  //   解释器走 config.python（PY_ENV 编码修正）。
  ctx.tools.register(defineTool({
    name: 'resource',
    description:
      '统一资源视图（只读）：一个入口访问多数据源。verb=ls 列举域内容 / read 读取引用 / search 检索。' +
      '命名空间：mem://（实体表 person/project/topic、规则条目、MEMORY.md/USER.md/DREAMS.md）、' +
      'wiki://（Obsidian 知识库目录与笔记）、scripts://（~/.dsh/scripts + ~/.hermes/scripts 脚本清单）。' +
      '引用格式 <ns>://<路径>，如 mem://（总览）、mem://person/句号（沿边+摘要）、mem://MEMORY.md、wiki://concepts。' +
      'search 为跨域检索（mem 语义召回 + wiki grep + 图谱沿边）；ls 加 filter 按子串过滤。' +
      '注意：深度语义召回请用 memory_recall；加密库请用 memory_vault_get；写操作请用原工具。',
    parameters: {
      verb: { type: 'string', required: true, description: 'ls（列举）/ read（读取）/ search（检索）' },
      ref: { type: 'string', description: '引用 <ns>://<路径>，如 mem://MEMORY.md、mem://（顶层）、wiki://concepts' },
      query: { type: 'string', description: 'search 的查询词（跨域时不带 ref）' },
      filter: { type: 'string', description: 'ls 结果按子串过滤（可选）' },
    },
    output: {
      schema: { type: 'string', description: '资源视图文本' },
      render: (args, value) => [{ type: 'text', text: value }],
    },
    async execute(input) {
      const verb = input?.verb
      if (!['ls', 'read', 'search'].includes(verb)) {
        return '[resource] verb 必须为 ls / read / search'
      }
      const args = [join(scriptsDir, 'resource.py'), '--verb', String(verb)]
      if (input?.ref) args.push('--ref', String(input.ref))
      if (input?.query) args.push('--query', String(input.query))
      if (input?.filter) args.push('--filter', String(input.filter))
      try {
        return execFileSync(PYTHON, args, {
          encoding: 'utf-8',
          timeout: 30000,
          maxBuffer: 2 * 1024 * 1024,
          env: PY_ENV,
        }).trimEnd()
      } catch (e) {
        const msg = e?.stderr || e?.message || String(e)
        return `[resource] 执行失败：${String(msg).slice(0, 300)}`
      }
    },
  }))

  // ---------- 6. 钩子：guard（tools/pre-execute 敏感扫描） ----------
  if (typeof ctx.on === 'function') {
    try {
      ctx.on('tools/pre-execute', (exec, next) => {
        const toolName = exec?.tool?.name || exec?.name
        if (!toolName || !WRITE_TOOLS.has(toolName)) return next()
        const args = exec?.arguments ?? exec?.args ?? {}
        const argsText = JSON.stringify(args)
        // v0.2.1：豁免只判「目标路径」字段——原实现对整段 arguments（含即将写入的正文）匹配，
        // 正文里出现 .env / credentials / .git/ 字样即整体跳过扫描（漏拦）。
        const targetPath = String(args.file_path || args.path || args.ref || '')
        if (EXEMPT_PATH.test(targetPath)) return next()
        const hit = SENSITIVE_PATTERNS.find((p) => p.re.test(argsText))
        if (hit) {
          if (guardBlock) {
            // 合法决策是 {kind:'deny'|...}（PreToolDecision）；原写的 'block' 不在联合类型内，
            // 只是被宿主"非 allow 且带 reason 即当拒绝"的兜底逻辑接受，属巧合可用。
            return { kind: 'deny', reason: `[memory-guard] 检测到敏感内容（${hit.label}），已拦截。请存入加密库（memory_vault_set）或只写引用路径。` }
          }
          state.pendingWarns.push(`[memory-guard] 检测到 ${hit.label} 写入，遵守"只存引用路径不存原文"：敏感内容请走 memory_vault_set，明文只留 → vault:<ref>。`)
        }
        return next()
      })
    } catch { /* guard 不可用则跳过 */ }

    // ---------- 7. 钩子：primer（agent/pre-step 门控召回注入） ----------
    if (ctx.agents) {
      const primerAuditFile = join(HOME, '.dsh', 'memories', 'primer-audit.jsonl')
      const appendPrimerAudit = ({ lane, q, chars }) => {
        try {
          mkdirSync(dirname(primerAuditFile), { recursive: true })
          appendFileSync(primerAuditFile, `${JSON.stringify({ ts: new Date().toISOString(), lane, q: q.slice(0, 50), chars })}\n`)
        } catch (e) {
          console.warn(`[dsh-memory] primer 审计写入失败: ${String(e?.message || e)}`)
        }
      }
      try {
        ctx.on('agent/pre-step', async (payload, next) => {
          const decision = await next()
          if (!decision || decision.kind === 'reject') return decision
          const warns = state.pendingWarns
          state.pendingWarns = []
          const lastUser = [...(decision.messages ?? [])].reverse().find((m) => m.role === 'user')
          const text = extractText(lastUser?.content)
          const gated = text && PRIMER_GATE.some((g) => text.includes(g))
          // delta 资产变更注入（2026-08-20）：最近 7 天变更资产清单，有变更才出声；
          // 目的：防"重复建设"（建文件前先感知已有资产），成本随变化量而非存量
          let deltaOut = ''
          try { const d = py('delta_assets.py', []); if (d.ok && d.out) deltaOut = d.out } catch { /* 注入失败不阻断 */ }
          if (!gated && warns.length === 0 && !deltaOut) return decision
          const parts = []
          if (warns.length) parts.push(warns.join('\n'))
          if (deltaOut) parts.push(`[资产变更提示·新建前先查重（可用 check_assets 工具）]\n${deltaOut}`)
          let primerLane = 'miss'
          if (gated) {
            const budget = classifyBudget(text) // 手术级注入分档（§8.5）
            const q = text.slice(0, 200)
            // fidelity-primer（借鉴 hermes）：先试定型速查卡，无卡自动降级 RRF；
            // --no-hits：primer 高频注入不污染命中统计
            const pc = py('rrf_recall.py', [q, '--primers', '--no-hits'])
            if (pc.ok && pc.out.startsWith('# 速查卡')) {
              primerLane = 'card'
              parts.push(`[事实速查卡·不可信参考资料，硬事实以卡为准]\n${pc.out}`)
            } else if (pc.ok && !pc.out.includes('（无命中）')) {
              primerLane = 'rrf'
              parts.push(`[记忆召回·不可信参考资料，硬事实须溯源一手材料]\n${pc.out}`)
            } else {
              const r = py('rrf_recall.py', [q, '--budget', String(budget), '--topk', String(primerTopK), '--no-hits'])
              if (r.ok && !r.out.includes('（无命中）')) {
                primerLane = 'rrf'
                parts.push(`[记忆召回·不可信参考资料，硬事实须溯源一手材料]\n${r.out}`)
              }
            }
          }
          if (parts.length === 0) {
            if (gated) appendPrimerAudit({ lane: primerLane, q: text, chars: 0 })
            return decision
          }
          try {
            const injectedText = parts.join('\n\n')
            const msg = await makeInjectedUserMessage({
              text: injectedText,
              kind: 'plugin:dsh-memory',
            })
            if (gated) appendPrimerAudit({ lane: primerLane, q: text, chars: injectedText.length })
            return { ...decision, messages: [...decision.messages, msg] }
          } catch (e) {
            // v0.2.1：注入失败整批回队（原实现 pendingWarns 已在上面清空 → 警告丢失且无重试），
            // 并留痕，避免"静默失效"再次发生。
            state.pendingWarns.unshift(...warns)
            console.warn(`[dsh-memory] primer 注入失败（已回队 ${warns.length} 条）: ${String(e?.message || e)}`)
            return decision
          }
        })
      } catch { /* pre-step 不可用则跳过 */ }
    }

    // ---------- 8. 钩子：harvest（会话转录；不可用则由 automation 兜底） ----------
    // v0.2.1：事件名修正——dsh 0.2 里 `turn/end` 只是**会话事件类型**（`session.append("turn/end")`），
    // 不是 Cordis 插件事件，`ctx.on('turn/end')` 永不触发。实据：`~/.dsh/memories/` 下从无
    // index.sqlite / harvest-state.json / corrections.jsonl，而 20 份会话日志都在（L2 通道恒空）。
    // 插件侧正确入口是 `session/event`（@mode emit，签名 `(session, event)`）。
    // 另：harvest 是重活（全量解析会话 zstd），改为**非阻塞 spawn + 每 10 分钟最多一次**，
    // 避免每个 turn 结束都同步阻塞主循环 30s。
    try {
      let lastHarvestAt = 0
      ctx.on('session/event', (_session, event) => {
        if (event?.type !== 'turn/end') return
        const now = Date.now()
        if (now - lastHarvestAt < 10 * 60 * 1000) return
        lastHarvestAt = now
        try {
          const child = spawn(PYTHON, [join(scriptsDir, 'harvest.py'), 'index'], {
            detached: true,
            stdio: 'ignore',
            env: PY_ENV,
          })
          child.on('error', () => { /* spawn 失败：交给 automation 兜底 */ })
          child.unref()
        } catch { /* 交给 automation 兜底 */ }
      })
    } catch { /* session/event 不可用：memory-harvest automation 兜底 */ }
  }

  return { memoryDir }
}
