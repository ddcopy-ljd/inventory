package com.yizhen.rfid

import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.json.JSONObject
import java.util.concurrent.TimeUnit

/**
 * 手持机盘点接口客户端（序列号方案）。
 * 服务端地址仅保存 origin（如 http://host:port），登录后保存 token。
 */
class ApiClient(private val prefs: Prefs) {

    private val client = OkHttpClient.Builder()
        .connectTimeout(8, TimeUnit.SECONDS)
        .readTimeout(15, TimeUnit.SECONDS)
        .build()

    private val jsonType = "application/json; charset=utf-8".toMediaType()

    val origin: String get() = prefs.origin
    val token: String get() = prefs.token
    val taskId: Int get() = prefs.taskId

    val isConfigured: Boolean get() = origin.isNotBlank() && token.isNotBlank()

    private fun url(path: String) = "$origin$path"

    private fun authHeader() = "Bearer $token"

    private fun post(path: String, body: JSONObject = JSONObject()): JSONObject {
        val req = Request.Builder().url(url(path))
            .addHeader("Authorization", authHeader())
            .post(body.toString().toRequestBody(jsonType)).build()
        client.newCall(req).execute().use { resp ->
            val text = resp.body?.string() ?: ""
            if (!resp.isSuccessful) throw RuntimeException("HTTP ${resp.code}: $text")
            return if (text.isBlank()) JSONObject() else JSONObject(text)
        }
    }

    private fun get(path: String): JSONObject {
        val req = Request.Builder().url(url(path))
            .addHeader("Authorization", authHeader()).get().build()
        client.newCall(req).execute().use { resp ->
            val text = resp.body?.string() ?: ""
            if (!resp.isSuccessful) throw RuntimeException("HTTP ${resp.code}: $text")
            return if (text.isBlank()) JSONObject() else JSONObject(text)
        }
    }

    /** 登录，保存 token。 */
    fun login(username: String, password: String): String {
        val body = JSONObject().put("username", username).put("password", password)
        val req = Request.Builder().url(url("/api/auth/login"))
            .post(body.toString().toRequestBody(jsonType)).build()
        client.newCall(req).execute().use { resp ->
            val text = resp.body?.string() ?: ""
            if (!resp.isSuccessful) throw RuntimeException("HTTP ${resp.code}: $text")
            val obj = JSONObject(text)
            val t = obj.optString("token")
            if (t.isBlank()) throw RuntimeException("登录失败：无 token")
            prefs.token = t
            return t
        }
    }

    /** 创建盘点任务，返回 {task_id, task_no, total, items:[{seq,epc}]} */
    fun createTask(): JSONObject {
        return post("/api/inventory/tasks")
    }

    /** 下载盘点清单，返回 {items:[{seq,epc}]} */
    fun getItems(taskId: Int): JSONObject {
        return get("/api/inventory/tasks/$taskId/items")
    }

    /**
     * 上报扫描结果。
     * @param seqs 已扫到的序号列表
     * @param unknownEpcs 不在清单中的 EPC 列表
     * @return {found, unknown}
     */
    fun scan(taskId: Int, seqs: List<Int>, unknownEpcs: List<String>): JSONObject {
        val body = JSONObject()
        val seqArr = JSONArray()
        seqs.forEach { seqArr.put(it) }
        val epcArr = JSONArray()
        unknownEpcs.forEach { epcArr.put(it) }
        body.put("seqs", seqArr).put("unknown_epcs", epcArr)
        return post("/api/inventory/tasks/$taskId/scan", body)
    }

    /** 完成盘点，返回 {task_id, surplus:[{epc}], shortage:[{code,name,epc}]} */
    fun finish(taskId: Int): JSONObject {
        return post("/api/inventory/tasks/$taskId/finish")
    }

    /** 取消盘点。 */
    fun cancel(taskId: Int): JSONObject {
        return post("/api/inventory/tasks/$taskId/cancel")
    }

    /** 查询库存是否被冻结。 */
    fun lockStatus(): JSONObject {
        return get("/api/inventory/lock-status")
    }

    companion object {
        const val QR_TASK = "task"    // 盘点任务二维码（含 origin）
        const val QR_UNKNOWN = "unknown"

        /** 识别扫码结果类型。 */
        fun qrKind(raw: String?): String {
            val s = (raw ?: "").trim()
            return if (s.startsWith("http://", ignoreCase = true) ||
                s.startsWith("https://", ignoreCase = true)) QR_TASK else QR_UNKNOWN
        }

        /** 补全 http(s) 协议头。 */
        fun normalize(raw: String?): String {
            val s = (raw ?: "").trim()
            return if (s.startsWith("http://", ignoreCase = true) ||
                s.startsWith("https://", ignoreCase = true)) s else "http://$s"
        }
    }
}
