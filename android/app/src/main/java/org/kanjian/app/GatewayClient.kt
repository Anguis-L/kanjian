package org.kanjian.app

import org.json.JSONObject
import java.io.DataOutputStream
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URL
import java.util.UUID

/**
 * 网关客户端（TDD §5）。
 *
 * 只做一件事：把一帧 JPEG + 元数据发到用户配置的地址，拿回结构化结果。
 * **不缓存图像、不写磁盘**（D-07）。
 *
 * 错误映射与网关一致：超时/不可用/限流/格式错误都要能被上层区分，
 * 这样 UI 才能说出「超时，可重试」而不是一句「失败」。
 */
class GatewayClient(private val baseUrl: String) {

    sealed class Outcome {
        data class Success(val result: SceneResult) : Outcome()
        data class Failure(val code: String, val message: String) : Outcome()
    }

    data class SceneResult(
        val sceneSummary: String,
        val speechCandidate: String,
        val objects: List<RecognizedObject>,
        val textRegions: List<String>,
        val uncertainties: List<String>,
        val imageQuality: String,
        val provider: String,
    )

    data class RecognizedObject(
        val label: String,
        val zone: String,
        val confidence: String,
    )

    private val configured: Boolean = baseUrl.isNotBlank()

    fun isConfigured(): Boolean = configured

    /** 阻塞调用，必须放在 IO 线程。 */
    fun describe(jpeg: ByteArray, maxSpeechChars: Int = 40): Outcome {
        if (!configured) {
            return Outcome.Failure("NOT_CONFIGURED", "未配置云端服务，仅本地可用")
        }

        val requestId = "req-" + UUID.randomUUID().toString().replace("-", "").take(16)
        val metadata = JSONObject().apply {
            put("schema_version", "1.0")
            put("request_id", requestId)
            put("session_id", "sess-" + UUID.randomUUID().toString().replace("-", "").take(12))
            put("frame_timestamp_ms", System.currentTimeMillis())
            put("origin", "USER")
            put("mode", "ASK")
            put("locale", "zh-CN")
            put("max_speech_chars", maxSpeechChars)
            put("policy_version", "2026-11-01")
            put("query", "前面是什么")
        }.toString()

        val boundary = "----kanjian" + UUID.randomUUID().toString().replace("-", "")
        val endpoint = baseUrl.trimEnd('/') + "/v1/scene:describe"

        var conn: HttpURLConnection? = null
        return try {
            conn = (URL(endpoint).openConnection() as HttpURLConnection).apply {
                requestMethod = "POST"
                connectTimeout = 3_000
                readTimeout = 15_000
                doOutput = true
                setRequestProperty("Content-Type", "multipart/form-data; boundary=$boundary")
            }

            DataOutputStream(conn.outputStream).use { out ->
                // metadata 部分
                out.writeBytes("--$boundary\r\n")
                out.writeBytes("Content-Disposition: form-data; name=\"metadata\"\r\n")
                out.writeBytes("Content-Type: application/json; charset=utf-8\r\n\r\n")
                out.write(metadata.toByteArray(Charsets.UTF_8))
                out.writeBytes("\r\n")
                // image 部分
                out.writeBytes("--$boundary\r\n")
                out.writeBytes("Content-Disposition: form-data; name=\"image\"; filename=\"frame.jpg\"\r\n")
                out.writeBytes("Content-Type: image/jpeg\r\n\r\n")
                out.write(jpeg)
                out.writeBytes("\r\n--$boundary--\r\n")
                out.flush()
            }

            val code = conn.responseCode
            if (code == 200) {
                val body = conn.inputStream.bufferedReader().use { it.readText() }
                Outcome.Success(parse(body))
            } else {
                val err = conn.errorStream?.bufferedReader()?.use { it.readText() } ?: ""
                mapError(code, err)
            }
        } catch (e: java.net.SocketTimeoutException) {
            Outcome.Failure("PROVIDER_TIMEOUT", "识别超时，可重试")
        } catch (e: IOException) {
            Outcome.Failure("NETWORK", "当前离线，无法云端识别")
        } finally {
            conn?.disconnect()
        }
    }

    private fun mapError(httpCode: Int, body: String): Outcome.Failure {
        val code = try {
            JSONObject(body).optJSONObject("error")?.optString("code").orEmpty()
        } catch (e: Exception) {
            ""
        }
        val message = when (code) {
            "PROVIDER_TIMEOUT" -> "识别超时，可重试"
            "RATE_LIMITED" -> "请求过于频繁，稍后重试"
            "IMAGE_TOO_LARGE" -> "图像过大"
            else -> when {
                httpCode == 429 -> "请求过于频繁，稍后重试"
                httpCode >= 500 -> "服务暂不可用"
                else -> "识别失败（$httpCode）"
            }
        }
        return Outcome.Failure(code.ifEmpty { "HTTP_$httpCode" }, message)
    }

    private fun parse(body: String): SceneResult {
        val json = JSONObject(body)
        val objects = mutableListOf<RecognizedObject>()
        json.optJSONArray("objects")?.let { arr ->
            for (i in 0 until arr.length()) {
                val o = arr.getJSONObject(i)
                objects.add(
                    RecognizedObject(
                        label = o.optString("label"),
                        zone = o.optString("zone", "unknown"),
                        confidence = o.optString("confidence", "unknown"),
                    )
                )
            }
        }
        val texts = mutableListOf<String>()
        json.optJSONArray("text_regions")?.let { arr ->
            for (i in 0 until arr.length()) {
                texts.add(arr.getJSONObject(i).optString("text"))
            }
        }
        val uncertainties = mutableListOf<String>()
        json.optJSONArray("uncertainties")?.let { arr ->
            for (i in 0 until arr.length()) uncertainties.add(arr.optString(i))
        }
        return SceneResult(
            sceneSummary = json.optString("scene_summary"),
            speechCandidate = json.optString("speech_candidate"),
            objects = objects,
            textRegions = texts,
            uncertainties = uncertainties,
            imageQuality = json.optString("image_quality", "unknown"),
            provider = json.optJSONObject("provider")?.optString("name").orEmpty(),
        )
    }
}
