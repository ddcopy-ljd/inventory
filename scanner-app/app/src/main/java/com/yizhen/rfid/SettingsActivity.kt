package com.yizhen.rfid

import android.os.Bundle
import android.widget.SeekBar
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.appcompat.widget.SwitchCompat
import android.widget.EditText
import android.widget.Spinner

class SettingsActivity : AppCompatActivity() {

    private lateinit var prefs: Prefs

    private lateinit var etUrl: EditText
    private lateinit var skPull: SeekBar
    private lateinit var tvPull: TextView
    private lateinit var skPower: SeekBar
    private lateinit var tvPower: TextView
    private lateinit var spRegion: Spinner
    private lateinit var spSession: Spinner
    private lateinit var spQ: Spinner
    private lateinit var spTrigger: Spinner
    private lateinit var swRssi: SwitchCompat
    private lateinit var skRssi: SeekBar
    private lateinit var tvRssi: TextView
    private lateinit var swDedup: SwitchCompat
    private lateinit var swHv: SwitchCompat
    private lateinit var swVibrate: SwitchCompat

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_settings)
        prefs = Prefs(this)
        bind()
        loadValues()

        findViewById<TextView>(R.id.btnBack).setOnClickListener { finish() }
        findViewById<TextView>(R.id.btnStSave).setOnClickListener { save() }
        findViewById<TextView>(R.id.btnReset).setOnClickListener {
            AlertDialog.Builder(this)
                .setMessage(R.string.st_confirm_reset)
                .setPositiveButton(android.R.string.ok) { _, _ -> resetUi() }
                .setNegativeButton(android.R.string.cancel, null)
                .show()
        }
    }

    private fun bind() {
        etUrl = findViewById(R.id.stUrl)
        skPull = findViewById(R.id.stPull)
        tvPull = findViewById(R.id.stPullVal)
        skPower = findViewById(R.id.stPower)
        tvPower = findViewById(R.id.stPowerVal)
        spRegion = findViewById(R.id.stRegion)
        spSession = findViewById(R.id.stSession)
        spQ = findViewById(R.id.stQValue)
        spTrigger = findViewById(R.id.stTriggerMode)
        swRssi = findViewById(R.id.stRssiEnabled)
        skRssi = findViewById(R.id.stRssiThreshold)
        tvRssi = findViewById(R.id.stRssiVal)
        swDedup = findViewById(R.id.stDedup)
        swHv = findViewById(R.id.stHvRemind)
        swVibrate = findViewById(R.id.stVibrate)

        val sec = getString(R.string.unit_seconds)
        skPull.setOnSeekBarChangeListener(object : SeekBar.OnSeekBarChangeListener {
            override fun onProgressChanged(sb: SeekBar?, p: Int, f: Boolean) {
                tvPull.text = "${p + 5} $sec"
            }
            override fun onStartTrackingTouch(sb: SeekBar?) {}
            override fun onStopTrackingTouch(sb: SeekBar?) {}
        })
        skPower.setOnSeekBarChangeListener(object : SeekBar.OnSeekBarChangeListener {
            override fun onProgressChanged(sb: SeekBar?, p: Int, f: Boolean) {
                tvPower.text = "${p + 5} dBm"
            }
            override fun onStartTrackingTouch(sb: SeekBar?) {}
            override fun onStopTrackingTouch(sb: SeekBar?) {}
        })
        skRssi.setOnSeekBarChangeListener(object : SeekBar.OnSeekBarChangeListener {
            override fun onProgressChanged(sb: SeekBar?, p: Int, f: Boolean) {
                tvRssi.text = "${p - 90} dBm"
            }
            override fun onStartTrackingTouch(sb: SeekBar?) {}
            override fun onStopTrackingTouch(sb: SeekBar?) {}
        })
    }

    private fun loadValues() {
        etUrl.setText(prefs.origin)
        skPull.progress = (prefs.pullInterval - 5).coerceIn(0, 115)
        skPower.progress = (prefs.power - 5).coerceIn(0, 25)
        spRegion.setSelection(prefs.region.coerceIn(0, 5))
        spSession.setSelection(prefs.session.coerceIn(0, 3))
        spQ.setSelection(prefs.qValue.coerceIn(0, 7))
        spTrigger.setSelection(prefs.triggerMode.coerceIn(0, 6))
        swRssi.isChecked = prefs.rssiEnabled
        skRssi.progress = (prefs.rssiThreshold + 90).coerceIn(0, 60)
        swDedup.isChecked = prefs.dedup
        swHv.isChecked = prefs.highValueRemind
        swVibrate.isChecked = prefs.vibrateAbnormal
        tvPull.text = "${prefs.pullInterval} ${getString(R.string.unit_seconds)}"
        tvPower.text = "${prefs.power} dBm"
        tvRssi.text = "${prefs.rssiThreshold} dBm"

        findViewById<TextView>(R.id.stDeviceId).text = prefs.deviceKey
        findViewById<TextView>(R.id.stSdkVer).text = "DeviceAPI 20220518"
        findViewById<TextView>(R.id.stFwVer).text = RfidManager.version().ifBlank { "—" }
    }

    private fun save() {
        prefs.origin = etUrl.text.toString().trim()
        prefs.pullInterval = skPull.progress + 5
        prefs.power = skPower.progress + 5
        prefs.region = spRegion.selectedItemPosition
        prefs.session = spSession.selectedItemPosition
        prefs.qValue = spQ.selectedItemPosition
        prefs.triggerMode = spTrigger.selectedItemPosition
        prefs.rssiEnabled = swRssi.isChecked
        prefs.rssiThreshold = skRssi.progress - 90
        prefs.dedup = swDedup.isChecked
        prefs.highValueRemind = swHv.isChecked
        prefs.vibrateAbnormal = swVibrate.isChecked
        RfidManager.setPower(prefs.power)
        Toast.makeText(this, R.string.settings_saved, Toast.LENGTH_SHORT).show()
        finish()
    }

    private fun resetUi() {
        prefs.resetAll()
        loadValues()
    }
}
