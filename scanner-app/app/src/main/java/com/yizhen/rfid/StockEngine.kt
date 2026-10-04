package com.yizhen.rfid

import org.json.JSONArray

/** 盘点引擎（序列号方案）。下载的清单仅含 {seq, epc}，本地建立 epc→seq 映射。 */
class StockEngine(private val prefs: Prefs) {

    /** epc → seq 映射 */
    private val epcToSeq = HashMap<String, Int>()
    val records = LinkedHashMap<String, ScanRecord>()

    var online = true
    private var seqCounter = 0

    val snapshotCount: Int get() = epcToSeq.size

    /** 加载盘点清单 [{seq, epc}]。 */
    fun loadSnapshot(json: String): Int {
        epcToSeq.clear()
        if (json.isBlank()) return 0
        val arr = JSONArray(json)
        for (i in 0 until arr.length()) {
            val o = arr.getJSONObject(i)
            val epc = o.optString("epc").uppercase()
            val seq = o.optInt("seq")
            if (epc.isNotEmpty() && seq > 0) {
                epcToSeq[epc] = seq
            }
        }
        return epcToSeq.size
    }

    /** 根据 epc 获取序号，不在清单中返回 null。 */
    fun seqOf(epc: String): Int? = epcToSeq[epc.uppercase()]

    /**
     * 本机扫到一个标签。
     * @return 1=本店商品(在清单中), 2=未知EPC(不在清单), 0=重复
     */
    fun onSelfScan(epcRaw: String, rssi: Int, time: String): Int {
        val epc = epcRaw.uppercase()
        val existing = records[epc]
        if (existing != null && existing.selfScanned) return 0

        val seq = epcToSeq[epc]
        return if (seq != null) {
            records[epc] = ScanRecord(
                epc = epc,
                type = RecordType.NORMAL,
                selfScanned = true,
                deviceNo = prefs.deviceNo,
                rssi = rssi,
                time = time,
                name = "",
                seq = ++seqCounter
            )
            1
        } else {
            records[epc] = ScanRecord(
                epc = epc,
                type = RecordType.ABNORMAL,
                selfScanned = true,
                deviceNo = prefs.deviceNo,
                rssi = rssi,
                time = time,
                name = "",
                seq = ++seqCounter
            )
            2
        }
    }

    /** 收集已扫到的序号列表（用于上报）。 */
    fun collectSeqs(): List<Int> {
        val result = ArrayList<Int>()
        for ((epc, rec) in records) {
            if (rec.selfScanned && rec.type != RecordType.ABNORMAL) {
                epcToSeq[epc]?.let { result.add(it) }
            }
        }
        return result
    }

    /** 收集未知 EPC 列表（不在清单中的）。 */
    fun collectUnknownEpcs(): List<String> {
        val result = ArrayList<String>()
        for ((epc, rec) in records) {
            if (rec.selfScanned && rec.type == RecordType.ABNORMAL) {
                result.add(epc)
            }
        }
        return result
    }

    fun listForTab(abnormalTab: Boolean): List<ScanRecord> {
        return records.values
            .filter { if (abnormalTab) it.type == RecordType.ABNORMAL else it.type != RecordType.ABNORMAL }
            .sortedWith(compareBy({ it.type.weight }, { -it.seq }))
    }

    fun countSelf(): Int = records.values.count { it.selfScanned }
    fun countAbnormal(): Int = records.values.count { it.type == RecordType.ABNORMAL }

    fun reset() {
        records.clear()
        epcToSeq.clear()
        online = true
        seqCounter = 0
    }
}
