package com.mikuagent.pet.brain

import android.content.Context
import android.content.SharedPreferences
import android.util.Log
import androidx.security.crypto.EncryptedSharedPreferences

/**
 * API Key 的落盘位置。
 *
 * 手机端能独立跑之后，App 里第一次出现「能花钱的凭据」（DeepSeek / MiniMax 的 Key）。
 * private prefs 别的 App 读不到，但在 rooted、备份、`adb` 场景下就是明文，所以优先
 * 用 [EncryptedSharedPreferences] 加密。
 *
 * 加密库初始化**可能失败**（个别 ROM 上 Tink/Keystore 会抛），而它的失败不该让
 * 整个 App 起不来 —— 那时退回普通 private prefs，并记一条日志。
 * 用户看到的差别只是「本机存储未加密」，功能不受影响。
 */
object SecretStore {

    private const val TAG = "SecretStore"
    private const val FILE = "miku_secrets"

    @Volatile
    private var cached: SharedPreferences? = null

    @Volatile
    private var encrypted = false

    fun get(context: Context): SharedPreferences {
        cached?.let { return it }
        synchronized(this) {
            cached?.let { return it }
            val app = context.applicationContext
            val prefs = try {
                // 用 `security-crypto:1.0.0` 的**字符串别名** API。
                // 1.0.0 里还没有 `MasterKey.Builder`（那是 1.1.0-alpha 才加的），
                // 引 alpha 不划算：这个重载在 1.0.0 就是正式接口，行为一样。
                val p = EncryptedSharedPreferences.create(
                    FILE,   // fileName
                    FILE,   // masterKeyAlias
                    app,
                    EncryptedSharedPreferences.PrefKeyEncryptionScheme.AES256_SIV,
                    EncryptedSharedPreferences.PrefValueEncryptionScheme.AES256_GCM,
                )
                encrypted = true
                p
            } catch (e: Exception) {
                Log.w(TAG, "加密存储不可用，退回普通 private prefs：${e.message}")
                encrypted = false
                app.getSharedPreferences(FILE, Context.MODE_PRIVATE)
            }
            cached = prefs
            return prefs
        }
    }

    /** 设置面板里显示「凭据是否加密存储」。 */
    fun isEncrypted(context: Context): Boolean {
        get(context)
        return encrypted
    }
}
