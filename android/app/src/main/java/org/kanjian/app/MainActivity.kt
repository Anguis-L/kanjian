package org.kanjian.app

import android.Manifest
import android.content.pm.PackageManager
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.os.Bundle
import android.speech.tts.TextToSpeech
import android.view.View
import android.widget.Button
import android.widget.TextView
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.camera.core.CameraSelector
import androidx.camera.core.ImageCapture
import androidx.camera.core.ImageCaptureException
import androidx.camera.core.ImageProxy
import androidx.camera.core.Preview
import androidx.camera.lifecycle.ProcessCameraProvider
import androidx.camera.view.PreviewView
import androidx.core.content.ContextCompat
import androidx.lifecycle.lifecycleScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.io.ByteArrayOutputStream
import java.util.concurrent.ExecutionException

/**
 * M1 垂直切片：拍摄 → 网关 → 端侧策略 → 播报。
 *
 * 刻意不做的事（避免原型期越界）：
 * - 不做自动观察（需 TriggerEngine + 采样，M2）
 * - 不做历史（M2）
 * - 不做预算 UI（M2）
 * - 不判断能不能走（永远不做）
 */
class MainActivity : AppCompatActivity() {

    private lateinit var previewView: PreviewView
    private lateinit var statusView: TextView
    private lateinit var btnAsk: Button
    private lateinit var btnStop: Button
    private lateinit var btnRepeat: Button

    private var imageCapture: ImageCapture? = null
    private var speech: SpeechOutput? = null
    private var gateway: GatewayClient? = null
    private var lastSpoken: String? = null

    private val prefs by lazy { getSharedPreferences("kanjian", MODE_PRIVATE) }

    private val requestPermission =
        registerForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
            if (granted) {
                startCamera()
            } else {
                setStatus(getString(R.string.perm_needed))
                speak(getString(R.string.perm_needed))
            }
        }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        previewView = findViewById(R.id.preview)
        statusView = findViewById(R.id.status)
        btnAsk = findViewById(R.id.btnAsk)
        btnStop = findViewById(R.id.btnStop)
        btnRepeat = findViewById(R.id.btnRepeat)

        // FR-ONB-001：首次启动必须确认安全与隐私说明，不可仅小字链接
        if (!prefs.getBoolean("onboarding_done", false)) {
            showOnboarding()
        }

        speech = SpeechOutput(
            onReady = {
                runOnUiThread { setStatus(getString(R.string.status_idle)) }
            },
            onFinished = {
                runOnUiThread { setStatus(getString(R.string.status_idle)) }
            },
            onUnavailable = {
                // 语音不可用时，状态文本是唯一的输出通道；
                // accessibilityLiveRegion=polite 让 TalkBack 仍能读出来（FR-ACC-002）。
                runOnUiThread {
                    setStatus(getString(R.string.tts_unavailable))
                    toast(getString(R.string.tts_unavailable))
                }
            }
        ).also { s ->
            val engine = TextToSpeech(this) { status ->
                s.onInit(status)
            }
            s.attach(engine)
        }

        val url = BuildConfig.GATEWAY_URL
        gateway = GatewayClient(url)
        if (url.isBlank()) {
            setStatus(getString(R.string.status_no_gateway))
        }

        btnAsk.setOnClickListener { askOnce() }
        btnStop.setOnClickListener {
            speech?.stopNow()
            setStatus(getString(R.string.status_idle))
        }
        btnRepeat.setOnClickListener {
            lastSpoken?.let { speak(it) } ?: toast("还没有可重复的描述")
        }

        if (hasCameraPermission()) startCamera() else requestPermission.launch(Manifest.permission.CAMERA)
    }

    // ---------------------------------------------------------------- 首次说明

    private fun showOnboarding() {
        AlertDialog.Builder(this)
            .setTitle(R.string.onboarding_title)
            .setMessage(R.string.onboarding_body)
            .setCancelable(false)
            .setPositiveButton(R.string.onboarding_confirm) { _, _ ->
                prefs.edit().putBoolean("onboarding_done", true).apply()
            }
            .setNegativeButton(R.string.onboarding_exit) { _, _ -> finish() }
            .show()
    }

    // ---------------------------------------------------------------- 相机

    private fun hasCameraPermission(): Boolean =
        ContextCompat.checkSelfPermission(this, Manifest.permission.CAMERA) ==
            PackageManager.PERMISSION_GRANTED

    private fun startCamera() {
        val future = ProcessCameraProvider.getInstance(this)
        future.addListener({
            try {
                val provider = future.get()
                val preview = Preview.Builder().build().also {
                    it.setSurfaceProvider(previewView.surfaceProvider)
                }
                imageCapture = ImageCapture.Builder()
                    .setCaptureMode(ImageCapture.CAPTURE_MODE_MINIMIZE_LATENCY)
                    .setTargetRotation(previewView.display.rotation)
                    .build()

                provider.unbindAll()
                provider.bindToLifecycle(
                    this, CameraSelector.DEFAULT_BACK_CAMERA, preview, imageCapture
                )
                setStatus(getString(R.string.status_idle))
            } catch (e: ExecutionException) {
                setStatus(getString(R.string.err_camera))
            }
        }, ContextCompat.getMainExecutor(this))
    }

    // ---------------------------------------------------------------- 主流程

    private fun askOnce() {
        val capture = imageCapture ?: run {
            setStatus(getString(R.string.err_camera))
            speak(getString(R.string.err_camera))
            return
        }
        val client = gateway ?: return
        if (!client.isConfigured()) {
            setStatus(getString(R.string.status_no_gateway))
            speak(getString(R.string.status_no_gateway))
            return
        }

        setStatus(getString(R.string.status_capturing))
        capture.takePicture(
            ContextCompat.getMainExecutor(this),
            object : ImageCapture.OnImageCapturedCallback() {
                override fun onCaptureSuccess(image: ImageProxy) {
                    val jpeg = imageToJpeg(image)
                    image.close()
                    if (jpeg == null) {
                        setStatus(getString(R.string.err_camera))
                        return
                    }
                    setStatus(getString(R.string.status_describing))
                    lifecycleScope.launch {
                        val outcome = withContext(Dispatchers.IO) { client.describe(jpeg) }
                        handleOutcome(outcome)
                    }
                }

                override fun onError(exception: ImageCaptureException) {
                    setStatus(getString(R.string.err_camera))
                }
            }
        )
    }

    private fun handleOutcome(outcome: GatewayClient.Outcome) {
        when (outcome) {
            is GatewayClient.Outcome.Failure -> {
                // 失败必须可感知，不能静默编造（PRD §2）
                display(outcome.message)
                speak(outcome.message)
            }

            is GatewayClient.Outcome.Success -> {
                val result = outcome.result
                // ADR-02：端侧是最终防线。网关已过滤过一遍，这里再过一遍。
                val policy = OutputPolicy.check(result.speechCandidate, maxChars = 40)
                val toSpeak = policy.text
                display(toSpeak)
                speak(toSpeak)
                lastSpoken = toSpeak
            }
        }
    }

    /**
     * 把文本放到状态栏。若语音不可用，附加明确标记——
     * 否则盲人用户会以为"按了没反应"，而实际上文字已经出来了。
     */
    private fun display(text: String) {
        val suffix = if (speech?.isReady == true) "" else "〔语音不可用，仅屏幕显示〕"
        setStatus(if (suffix.isEmpty()) text else "$text $suffix")
    }

    // ---------------------------------------------------------------- 工具

    private fun speak(text: String) {
        val ok = speech?.speak(text) ?: false
        if (!ok) {
            // 不静默失败：文字已在状态栏，明确告知语音通道缺失
            toast(getString(R.string.tts_unavailable))
        } else {
            setStatus(getString(R.string.status_speaking))
            // 播报结束后状态栏保留刚才那句话，便于重复播放与屏幕确认
            statusView.postDelayed({ setStatus(text) }, 800)
        }
    }

    private fun setStatus(text: String) {
        statusView.text = text
    }

    private fun toast(msg: String) {
        Toast.makeText(this, msg, Toast.LENGTH_SHORT).show()
    }

    /** ImageProxy → 压缩 JPEG，长边 ≤1280、质量 80（TDD §6）。 */
    private fun imageToJpeg(image: ImageProxy): ByteArray? {
        return try {
            val buffer = image.planes[0].buffer
            val bytes = ByteArray(buffer.remaining()).also { buffer.get(it) }
            var bitmap = BitmapFactory.decodeByteArray(bytes, 0, bytes.size) ?: return null
            bitmap = scaleDown(bitmap, 1280)
            val out = ByteArrayOutputStream()
            bitmap.compress(Bitmap.CompressFormat.JPEG, 80, out)
            bitmap.recycle()
            out.toByteArray()
        } catch (e: Exception) {
            null
        }
    }

    private fun scaleDown(src: Bitmap, maxEdge: Int): Bitmap {
        val w = src.width
        val h = src.height
        val longEdge = maxOf(w, h)
        if (longEdge <= maxEdge) return src
        val scale = maxEdge.toFloat() / longEdge
        val scaled = Bitmap.createScaledBitmap(src, (w * scale).toInt(), (h * scale).toInt(), true)
        if (scaled !== src) src.recycle()
        return scaled
    }

    override fun onDestroy() {
        speech?.shutdown()
        super.onDestroy()
    }
}
