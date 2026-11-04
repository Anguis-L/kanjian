package org.kanjian.app

import android.speech.tts.TextToSpeech
import android.speech.tts.UtteranceProgressListener
import java.util.Locale
import java.util.UUID

/**
 * 单通道语音输出（FR-TTS-001/002）。
 *
 * 设计约束（SDD §9、ADR-11）：
 * - 同一时刻只播一条
 * - 停止要能立即打断，且清空待播
 * - 首版不请求独占音频焦点：DUCK 而非静音，避免遮盖交通声
 *
 * 原型阶段只做「单条 + 可停止」这条最小必要路径；队列/去重/冷却
 * 由上层 SpeechScheduler 承担，这里不重复实现。
 */
class SpeechOutput(
    private val onReady: () -> Unit,
    private val onFinished: () -> Unit,
    private val onUnavailable: () -> Unit = {},
) : TextToSpeech.OnInitListener {

    private var tts: TextToSpeech? = null
    private var ready = false
    private var currentUtteranceId: String? = null

    override fun onInit(status: Int) {
        if (status == TextToSpeech.SUCCESS) {
            val engine = tts
            engine?.let {
                // 中文语音包缺失时不要假装成功——这是必须暴露的降级点
                val langResult = it.setLanguage(Locale.SIMPLIFIED_CHINESE)
                ready = langResult != TextToSpeech.LANG_MISSING_DATA &&
                    langResult != TextToSpeech.LANG_NOT_SUPPORTED
                it.setOnUtteranceProgressListener(object : UtteranceProgressListener() {
                    override fun onStart(utteranceId: String?) {}
                    override fun onDone(utteranceId: String?) {
                        if (utteranceId == currentUtteranceId) {
                            currentUtteranceId = null
                            onFinished()
                        }
                    }

                    @Deprecated("Deprecated in Java")
                    override fun onError(utteranceId: String?) {
                        currentUtteranceId = null
                        onFinished()
                    }
                })
            }
            if (ready) onReady() else onUnavailable()
        } else {
            // 初始化失败（无 TTS 引擎/被禁用）：必须让用户知道，
            // 否则盲人用户会以为"按了没反应"。
            ready = false
            onUnavailable()
        }
    }

    fun attach(engine: TextToSpeech) {
        this.tts = engine
    }

    val isReady: Boolean get() = ready

    /** 播报一条文本。返回是否真正交给了 TTS。 */
    fun speak(text: String): Boolean {
        val engine = tts ?: return false
        if (!ready) return false
        stopNow()  // 严格单通道：先停再播
        val id = UUID.randomUUID().toString()
        currentUtteranceId = id
        engine.speak(text, TextToSpeech.QUEUE_FLUSH, null, id)
        return true
    }

    /** 立即停止并清空（FR-TTS-002）。 */
    fun stopNow() {
        currentUtteranceId = null
        tts?.stop()
    }

    fun shutdown() {
        stopNow()
        tts?.shutdown()
        tts = null
        ready = false
    }
}
