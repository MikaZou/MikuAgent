package com.mikuagent.pet.web

import android.content.Context
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.util.Log
import androidx.webkit.WebViewAssetLoader
import java.io.ByteArrayInputStream
import java.io.ByteArrayOutputStream
import java.io.File
import java.io.FileInputStream
import java.io.IOException

/**
 * 本地资源服务：把 WebView 要的东西从「本地文件」喂进去，让它完全不碰网络。
 *
 * 存在的三个理由：
 *  1. **零 CDN** —— pixi / cubism4 / Cubism Core 全部打包进 APK 的 assets，
 *     不再依赖 jsdelivr 和 cubism.live2d.com（旧版 phone.html 的死因之一）。
 *  2. **模型不进 APK** —— 模型授权写明「不可二传二改」，所以模型放在运行时
 *     拉取到本地的目录里，由这里读出来喂给 WebView。
 *  3. **贴图降采样** —— 新模型是 6 张 4096²，未压缩 RGBA 要 384 MB 显存。
 *     在 [textureScale] < 1 时用 [BitmapFactory.inSampleSize] 直接把贴图
 *     在**原生侧**缩小再交给 WebGL，这是控制显存最有效的一刀。
 *
 * 页面源是 `https://appassets.androidplatform.net/...`，这是 WebViewAssetLoader
 * 约定的本地域，用它是因为 **https 才是安全上下文**（浏览器版 phone.html
 * 就是因为 http://192.168.x.x 拿不到麦克风）。
 */
class AssetServer(private val context: Context) {

    /**
     * 贴图缩放系数。1.0 = 原图 4096（384 MB 显存，风险高）；
     * 0.5 = 2048（约 96 MB）。
     *
     * 由 MainActivity 在 WebGL 上下文丢失 / OOM 时下调后重建。
     */
    @Volatile
    var textureScale: Double = 1.0

    /** 模型目录。优先应用私有目录，其次外部私有目录（方便 adb push 联调）。 */
    val modelDir: File
        get() {
            val internal = File(context.filesDir, MODEL_DIR_NAME)
            if (internal.isDirectory) return internal
            val external = context.getExternalFilesDir(null)?.let { File(it, MODEL_DIR_NAME) }
            if (external != null && external.isDirectory) return external
            return internal   // 不存在时返回内部路径，交给 ModelSync 去创建
        }

    private val loader: WebViewAssetLoader = WebViewAssetLoader.Builder()
        .setDomain(DOMAIN)
        .addPathHandler("/assets/", WebViewAssetLoader.AssetsPathHandler(context))
        .addPathHandler("/model/", ModelPathHandler())
        .build()

    fun shouldInterceptRequest(url: android.net.Uri): android.webkit.WebResourceResponse? =
        loader.shouldInterceptRequest(url)

    /** 入口页面地址（index.html 只引本地 lib/，不发任何外部请求）。 */
    fun indexUrl(): String = "https://$DOMAIN/assets/web/index.html"

    /**
     * `/model/` 前缀的处理器。
     *
     * 路径做了防目录穿越：只允许落在 [modelDir] 之内，避免 `../` 读到别处。
     */
    private inner class ModelPathHandler : WebViewAssetLoader.PathHandler {
        override fun handle(path: String): android.webkit.WebResourceResponse? {
            val root = modelDir.canonicalFile
            val target = File(root, path).canonicalFile

            if (!target.path.startsWith(root.path + File.separator) && target != root) {
                Log.w(TAG, "拒绝目录穿越: $path")
                return notFound()
            }
            if (!target.isFile) {
                Log.w(TAG, "模型文件不存在: ${target.absolutePath}")
                return notFound()
            }

            return try {
                when {
                    // 贴图按需降采样（控制 384MB 显存的主要手段）
                    textureScale < 0.999 && target.extension.equals("png", true) ->
                        serveScaledPng(target)

                    // model3.json 需要在**内存里**补全表情/动作，见 patchModelJson
                    target.name.endsWith(".model3.json") ->
                        servePatchedModelJson(target)

                    else -> serveRaw(target)
                }
            } catch (e: IOException) {
                Log.e(TAG, "读取模型文件失败: ${target.name}", e)
                notFound()
            }
        }
    }

    /**
     * 在**内存里**给 model3.json 补上 Expressions / Motions，磁盘文件一个字节都不动。
     *
     * 为什么需要：这个模型（`D:\game\miku`）的 `model3.json` 里
     * `Motions` 和 `Expressions` **都是空的** —— 9 个表情是独立的 `.exp3` 文件，
     * VTube Studio 靠自己的 `miku.vtube.json` 热键表去加载它们。标准 Cubism
     * 运行时不会自动发现这些文件，于是 `model.expression("圈圈")` 找不到东西。
     *
     * 模型授权写明「不可二传二改」，所以**不能改盘上的 json**；
     * 这里只改喂给 WebView 的那份副本。
     */
    private fun servePatchedModelJson(file: File): android.webkit.WebResourceResponse {
        val raw = file.readText(Charsets.UTF_8)
        val patched = try {
            patchModelJson(raw, file.parentFile)
        } catch (e: Exception) {
            Log.w(TAG, "补全 model3.json 失败，回退原始内容：${e.message}")
            raw
        }
        return android.webkit.WebResourceResponse(
            null, "application/json", ByteArrayInputStream(patched.toByteArray(Charsets.UTF_8))
        ).apply { setStatusCodeAndReasonPhrase(200, "OK") }
    }

    private fun patchModelJson(raw: String, dir: File?): String {
        val root = org.json.JSONObject(raw)
        val refs = root.optJSONObject("FileReferences")
            ?: org.json.JSONObject().also { root.put("FileReferences", it) }

        // ---- 表情：从 vtube.json 的热键表还原 ----
        val haveExp = refs.optJSONArray("Expressions")
        if (haveExp == null || haveExp.length() == 0) {
            val arr = org.json.JSONArray()
            for ((name, f) in readVtubeExpressions(dir)) {
                arr.put(org.json.JSONObject().put("Name", name).put("File", f))
            }
            if (arr.length() > 0) {
                refs.put("Expressions", arr)
                Log.i(TAG, "补全 ${arr.length()} 个表情：${readVtubeExpressions(dir).joinToString { it.first }}")
            }
        }

        // ---- 动作：目录里扫到的 *.motion3.json 挂到 Idle 组 ----
        val haveMot = refs.optJSONObject("Motions")
        if (haveMot == null || haveMot.length() == 0) {
            val files = dir?.listFiles { f -> f.isFile && f.name.endsWith(".motion3.json") } ?: emptyArray()
            if (files.isNotEmpty()) {
                val arr = org.json.JSONArray()
                for (f in files) arr.put(org.json.JSONObject().put("File", f.name))
                refs.put("Motions", org.json.JSONObject().put("Idle", arr))
                Log.i(TAG, "补全 ${files.size} 个动作：${files.joinToString { it.name }}")
            }
        }

        return root.toString()
    }

    /**
     * 读 `miku.vtube.json` 的 Hotkeys，取出 Action == ToggleExpression 的条目。
     *
     * 返回 (显示名, exp3 文件名)。显示名是中文（圈圈/脸红/前倾/葱/唱歌/比心/QQ人/水印），
     * 与模型作者在 VTS 里的按键名一致。
     */
    private fun readVtubeExpressions(dir: File?): List<Pair<String, String>> {
        if (dir == null) return emptyList()
        val vtube = dir.listFiles { f -> f.isFile && f.name.endsWith(".vtube.json") }?.firstOrNull()
            ?: return emptyList()
        return try {
            val root = org.json.JSONObject(vtube.readText(Charsets.UTF_8))
            val hotkeys = root.optJSONArray("Hotkeys") ?: return emptyList()
            val out = ArrayList<Pair<String, String>>()
            for (i in 0 until hotkeys.length()) {
                val h = hotkeys.getJSONObject(i)
                if (h.optString("Action") != "ToggleExpression") continue
                val file = h.optString("File")
                val name = h.optString("Name").ifBlank { file.substringBeforeLast('.') }
                if (file.isNotBlank()) out.add(name to file)
            }
            out
        } catch (e: Exception) {
            Log.w(TAG, "解析 vtube.json 失败：${e.message}")
            emptyList()
        }
    }

    private fun serveRaw(file: File): android.webkit.WebResourceResponse {
        val mime = mimeOf(file.name)
        return android.webkit.WebResourceResponse(
            null, mime, FileInputStream(file)
        ).apply {
            setStatusCodeAndReasonPhrase(200, "OK")
            // moc3 / json 会随模型更新变化，明文缓存即可；这里不设缓存头，
            // 交给 WebView 默认行为，避免降采样切换后拿到旧图。
        }
    }

    /**
     * 把 PNG 解码后按 [textureScale] 缩小，再编码回 PNG。
     *
     * 用 `inSampleSize`（只能取 2 的幂）先把解码阶段的内存压下来 —— 关键是
     * **不要**先解出完整的 4096² Bitmap 再缩，那样峰值内存一样会爆。
     */
    private fun serveScaledPng(file: File): android.webkit.WebResourceResponse {
        val sample = if (textureScale <= 0.5) 2 else 1

        val bounds = BitmapFactory.Options().apply { inJustDecodeBounds = true }
        BitmapFactory.decodeFile(file.absolutePath, bounds)

        val opts = BitmapFactory.Options().apply {
            inSampleSize = sample
            inPreferredConfig = Bitmap.Config.ARGB_8888
        }
        val bmp = BitmapFactory.decodeFile(file.absolutePath, opts)
            ?: return notFound()

        // inSampleSize 只有 2 的幂，剩下的比例用 createScaledBitmap 补齐
        val wantW = (bounds.outWidth * textureScale).toInt().coerceAtLeast(1)
        val scaled = if (bmp.width != wantW && wantW < bmp.width) {
            Bitmap.createScaledBitmap(bmp, wantW, (bmp.height.toDouble() * wantW / bmp.width).toInt(), true)
                .also { if (it != bmp) bmp.recycle() }
        } else bmp

        val out = ByteArrayOutputStream()
        scaled.compress(Bitmap.CompressFormat.PNG, 100, out)
        scaled.recycle()

        Log.i(TAG, "贴图降采样 ${file.name}: ${bounds.outWidth} -> ${wantW} (sample=$sample, ${out.size() / 1024} KB)")
        return android.webkit.WebResourceResponse(
            null, "image/png", ByteArrayInputStream(out.toByteArray())
        ).apply { setStatusCodeAndReasonPhrase(200, "OK") }
    }

    private fun notFound() = android.webkit.WebResourceResponse(
        null, "text/plain", ByteArrayInputStream("not found".toByteArray())
    ).apply { setStatusCodeAndReasonPhrase(404, "Not Found") }

    private fun mimeOf(name: String): String = when (name.substringAfterLast('.', "").lowercase()) {
        "html" -> "text/html"
        "js" -> "application/javascript"
        "css" -> "text/css"
        "json" -> "application/json"
        "png" -> "image/png"
        "jpg", "jpeg" -> "image/jpeg"
        "moc3" -> "application/octet-stream"
        else -> "application/octet-stream"
    }

    companion object {
        private const val TAG = "AssetServer"

        /** WebViewAssetLoader 约定的本地域；必须是 https 才是安全上下文。 */
        const val DOMAIN = "appassets.androidplatform.net"

        /** 模型目录名，与 PC 端 REMOTE_MODEL_DIR 的 basename 保持一致。 */
        const val MODEL_DIR_NAME = "miku_v5"
    }
}
