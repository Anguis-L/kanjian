package org.kanjian.app

/**
 * 端侧输出策略（ADR-02：端侧拥有最终输出权）。
 *
 * 这是 Python 版 `kanjian/policy.py` 的 Kotlin 移植，两边必须保持一致：
 * 同一组对抗样本在两端都要被拦截。移植时只保留**行为**，不追求字面相同。
 *
 * 设计约束与 Python 版一致：
 * - 规则按语义类别组织，不是单一禁词表
 * - 宁可拒绝，不把违规文本"修"成另一句可能更危险的话
 * - 硬阻断（身份/敏感属性/注入）整条拒绝，软阻断可删从句
 */
object OutputPolicy {

    enum class Category {
        ACTION_DIRECTIVE, PASSABILITY, SAFETY_CONCLUSION,
        PRECISE_DISTANCE, IDENTITY, SENSITIVE_ATTRIBUTE,
        EXHAUSTIVE, PROMPT_INJECTION,
    }

    data class Violation(val category: Category, val matched: String)

    data class Result(
        val allowed: Boolean,
        val text: String,
        val violations: List<Violation> = emptyList(),
        val reason: String = "",
    )

    /** 拒绝时的兜底文案（PRD §8.1）。 */
    const val REJECT_TEXT = "无法可靠描述。"

    private const val MIN_KEEP_CHARS = 4

    /** 硬阻断：整条拒绝，不允许删从句保留其余。 */
    private val HARD_BLOCK = setOf(
        Category.IDENTITY, Category.SENSITIVE_ATTRIBUTE, Category.PROMPT_INJECTION
    )

    private val ZERO_WIDTH = Regex("[\u200B\u200C\u200D\u200E\u200F\uFEFF\u2060]")
    private val CJK_GAP = Regex("(?<=[\\u4e00-\\u9fff])\\s+(?=[\\u4e00-\\u9fff])")
    private val MULTI_SPACE = Regex("[ \\t\\u3000]+")
    private val CLAUSE = Regex("[^，。；、,.!?;\\n]+[，。；、,.!?;]?")

    private data class Rule(val category: Category, val regex: Regex)

    // 注意：Kotlin 的 \u4e00 在字符串里需转义为 \\u4e00（正则字面量），此处直接写中文范围。
    private val RULES: List<Rule> = listOf(
        // ---- 行动指令 ----
        Rule(Category.ACTION_DIRECTIVE, Regex("(可以|可|能|能够|您能|你能)[^。!?\\n]{0,6}(走|过|通行|通过|穿过|沿|往前|向左|向右|绕开|绕过去|绕行|前进|下楼|上楼)")),
        Rule(Category.ACTION_DIRECTIVE, Regex("(向|往|朝)(左|右|前|后|左侧|右侧|前方|后方)[^。!?\\n]{0,4}(走|绕|转|拐|移动|退|进)")),
        Rule(Category.ACTION_DIRECTIVE, Regex("(绕开|绕过去|绕过|躲开|闪开|避开)[^。!?\\n]{0,6}(它|他|她|障碍|车|人)?")),
        Rule(Category.ACTION_DIRECTIVE, Regex("(请|建议|应该|最好|需要|务必)(你|您)?[^。!?\\n]{0,4}(走|往|向|绕|停|退|加速|减速)")),
        Rule(Category.ACTION_DIRECTIVE, Regex("(往前走|继续走|继续前进|直行|前行|停下|停住|退后|后退)(吧|哦|啊)?")),
        Rule(Category.ACTION_DIRECTIVE, Regex("(go ahead|(you |we )?(can|should|may|must) (go|walk|proceed|pass|move)|please (walk|go|move|proceed)|(walk|go|move) (forward|ahead|straight)|(turn|move|step) (left|right|forward|back))")),

        // ---- 通行可行性（正负都算）----
        Rule(Category.PASSABILITY, Regex("(能|可以|可|无法|不能|不可以|过不去|走不过去|通不过|走不通|走得通|过得了|过去不了)[^。!?\\n]{0,3}(走|过去|通过|通行|穿过)")),
        Rule(Category.PASSABILITY, Regex("(过不去|走不过去|过不了|通不过|走不通)")),
        Rule(Category.PASSABILITY, Regex("(can(not)? (walk|pass|proceed|get through)|(not |un)?(passable|walkable|impassable))")),

        // ---- 安全结论 ----
        Rule(Category.SAFETY_CONCLUSION, Regex("(没有|没|无|不存在|未发现|未检出)[^。!?\\n]{0,4}(障碍|障碍物|危险|风险|车|车辆|来人|人)")),
        Rule(Category.SAFETY_CONCLUSION, Regex("(现在|当前|这里|此处|前方|前面)?(是)?安全(的)?(通过|通行|走|过|地点|区域)?")),
        Rule(Category.SAFETY_CONCLUSION, Regex("(安全(地)?(通过|通行|穿越|走过)|可以(安全)?(过马路|穿越|横穿))")),
        Rule(Category.SAFETY_CONCLUSION, Regex("(路上|路面|道路|通道)(没有|没|无)")),
        Rule(Category.SAFETY_CONCLUSION, Regex("(no (obstacle|danger|car|vehicle|traffic)|all clear|it'?s safe|safe to (cross|walk|pass|go))")),

        // ---- 穷尽性 ----
        Rule(Category.EXHAUSTIVE, Regex("(全部|所有|一切|任何|每个|每一)[^。!?\\n]{0,4}(物体|东西|物品|障碍|人|车|风险)")),
        Rule(Category.EXHAUSTIVE, Regex("(没有|没|无)(任何)?(遗漏|漏掉|漏报|其他物体|别的东西)")),
        Rule(Category.EXHAUSTIVE, Regex("(已|都|全部)(确认|检查|排查)(完毕|过|清楚|到位)")),
        Rule(Category.EXHAUSTIVE, Regex("(everything|all (objects|items)|nothing else|no other)")),

        // ---- 精确距离 ----
        Rule(Category.PRECISE_DISTANCE, Regex("\\d+(\\.\\d+)?\\s*(米|m|厘米|cm|毫米|mm|步|码|英尺|公里|km)")),
        Rule(Category.PRECISE_DISTANCE, Regex("[零一二三四五六七八九十两半百]+\\s*(米|厘米|步|码)")),
        Rule(Category.PRECISE_DISTANCE, Regex("\\d+(\\.\\d+)?\\s*(meters?|metres?|feet|ft|inches?|steps?|yards?|kilometers?)")),

        // ---- 身份识别 ----
        Rule(Category.IDENTITY, Regex("(这是|可能是|应该是|像是)[^。!?\\n]{0,4}(你|您)的(家人|亲人|朋友|熟人|妻子|丈夫|老公|老婆|儿子|女儿|父母|同事)")),
        Rule(Category.IDENTITY, Regex("(识别|认出|确认)(出|到|了)?[^。!?\\n]{0,4}(是|为)?[^。!?\\n]{0,6}(本人|熟人|朋友|家人)")),
        Rule(Category.IDENTITY, Regex("(this is|that is) (your|a) (family|friend|wife|husband|son|daughter|colleague)")),

        // ---- 敏感属性 ----
        Rule(Category.SENSITIVE_ATTRIBUTE, Regex("(他|她|此人|这个人|那个人|对方|那人|那个男的|那个女的|这位|那位|老人|男子|女子)[^。!?\\n]{0,4}(正在|看起来|显得|似乎|应该|好像)[^。!?\\n]{0,4}(生气|愤怒|悲伤|难过|高兴|开心|害怕|恐惧|紧张|焦虑|醉|喝醉|生病|不舒服)")),
        Rule(Category.SENSITIVE_ATTRIBUTE, Regex("(年龄|岁数)(大约|大概|约)?[^。!?\\n]{0,4}\\d+")),
        Rule(Category.SENSITIVE_ATTRIBUTE, Regex("(残疾人|盲人|瘸子|瞎子|乞丐|流浪汉)")),
        // 英文敏感属性：移植时曾漏掉这一条，被一致性测试抓到（This person looks angry.）
        Rule(Category.SENSITIVE_ATTRIBUTE, Regex("(this person|he|she|that person|the man|the woman)\\s+(is|looks|seems|appears)\\s+(angry|sad|happy|scared|afraid|drunk|sick|old|young|upset|nervous)")),

        // ---- 提示注入 ----
        Rule(Category.PROMPT_INJECTION, Regex("(忽略|无视|忘记|ignore)[^。!?\\n]{0,6}(之前|以上|上述|前面|prior|previous|above|all)[^。!?\\n]{0,6}(规则|指令|提示|要求|rules|instructions?|prompts?)")),
        Rule(Category.PROMPT_INJECTION, Regex("(system|assistant)\\s*(prompt|message)")),
        Rule(Category.PROMPT_INJECTION, Regex("(告诉|通知|让)(用户|使用者|他|她)[^。!?\\n]{0,6}(可以走|能走|安全|通行)")),
    )

    /** 归一化：全角转半角、去零宽、去中文间空白、小写。 */
    fun normalize(input: String): String {
        var t = input
        t = ZERO_WIDTH.replace(t, "")
        t = t.map { ch ->
            when {
                ch.code in 0xFF01..0xFF5E -> (ch.code - 0xFEE0).toChar() // 全角 ASCII
                ch == '\u3000' -> ' '
                else -> ch
            }
        }.joinToString("")
        t = CJK_GAP.replace(t, "")
        t = MULTI_SPACE.replace(t, " ")
        return t.trim().lowercase()
    }

    fun findViolations(text: String): List<Violation> {
        val norm = normalize(text)
        if (norm.isEmpty()) return emptyList()
        val out = LinkedHashSet<Violation>()
        for (rule in RULES) {
            for (m in rule.regex.findAll(norm)) {
                val matched = m.value.trim()
                if (matched.isNotEmpty()) out.add(Violation(rule.category, matched))
            }
        }
        return out.toList()
    }

    /**
     * 对候选播报文本执行策略判定。
     * @param maxChars 播报长度上限，来自端侧配置（FR-CFG-001）。
     */
    fun check(text: String, maxChars: Int = 40): Result {
        val norm = normalize(text)
        if (norm.isEmpty()) return Result(false, REJECT_TEXT, reason = "EMPTY")

        val violations = findViolations(text)
        if (violations.isEmpty()) {
            return Result(true, enforceLength(norm, maxChars))
        }

        val hard = violations.filter { it.category in HARD_BLOCK }
        if (hard.isNotEmpty()) {
            return Result(false, REJECT_TEXT, hard, "HARD_BLOCK:${hard.first().category}")
        }

        // 软阻断：删掉违规从句，再复查残留（与 Python 版一致：裁剪不是终点）
        val badClauses = violations.map { clauseAt(norm, it.matched) }.toSet()
        var kept = splitClauses(norm).filter { it.isNotBlank() && it.trim() !in badClauses }.joinToString("")

        repeat(4) {
            val residual = findViolations(kept)
            if (residual.isEmpty()) return@repeat
            val rc = residual.map { clauseAt(kept, it.matched) }.toSet()
            kept = splitClauses(kept).filter { it.isNotBlank() && it.trim() !in rc }.joinToString("")
        }

        if (meaningfulLen(kept) < MIN_KEEP_CHARS || findViolations(kept).isNotEmpty()) {
            return Result(false, REJECT_TEXT, violations, "NO_RELIABLE_FACT_LEFT")
        }
        return Result(true, enforceLength(kept, maxChars), violations, "CLAUSE_STRIPPED")
    }

    /** OCR 通道：图像文字是不可信数据，注入内容不得朗读（SDD §8）。 */
    fun checkOcrText(text: String): Result {
        val norm = normalize(text)
        if (norm.isEmpty()) return Result(false, "", reason = "EMPTY")
        val injection = findViolations(text).filter { it.category == Category.PROMPT_INJECTION }
        if (injection.isNotEmpty()) {
            return Result(false, "", injection, "OCR_PROMPT_INJECTION")
        }
        return Result(true, text)
    }

    // ---- 内部工具 ----

    private fun splitClauses(s: String): List<String> = CLAUSE.findAll(s).map { it.value }.toList()

    private fun clauseAt(norm: String, matched: String): String {
        val idx = norm.indexOf(matched)
        if (idx < 0) return norm.trim()
        for (c in splitClauses(norm)) {
            val start = norm.indexOf(c)
            if (start <= idx && idx < start + c.length) return c.trim()
        }
        return norm.trim()
    }

    private fun meaningfulLen(s: String): Int =
        s.count { it.isLetterOrDigit() || it in '\u4e00'..'\u9fff' }

    /** 超长按从句截断，不靠倍速播放（SDD §9）。 */
    private fun enforceLength(text: String, maxChars: Int): String {
        val display = toDisplay(text)
        if (display.length <= maxChars) return display
        val sb = StringBuilder()
        for (clause in splitClauses(display)) {
            if (sb.length + clause.length > maxChars) break
            sb.append(clause)
        }
        val out = sb.toString().trimEnd('，', '。', '；', '、', '：', ' ')
        if (out.isEmpty()) return display.take(maxChars).trim()
        return if (out.last() in "。！？") out else "$out。"
    }

    /** 归一化把全角转半角，播报前必须还原中文标点（TTS 依赖它断句）。 */
    private fun toDisplay(text: String): String {
        val map = mapOf(
            ',' to '，', '.' to '。', ';' to '；', ':' to '：',
            '?' to '？', '!' to '！', '(' to '（', ')' to '）', ' ' to ' ',
        )
        val sb = StringBuilder()
        for (ch in text) {
            val c = map[ch] ?: ch
            if (c == ' ') continue
            // 收敛重复标点
            if (sb.isNotEmpty() && sb.last() == c && c in "，。；：") continue
            sb.append(c)
        }
        var out = sb.toString().trim('，', '；', '：', ' ')
        if (out.isNotEmpty() && out.last() !in "。！？") out += "。"
        return out
    }
}
