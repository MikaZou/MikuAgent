package com.mikuagent.pet.model

import android.content.Context
import android.util.Log
import okhttp3.OkHttpClient
import okhttp3.Request
import org.json.JSONObject
import java.io.File
import java.security.MessageDigest
import java.util.concurrent.TimeUnit

/**
 * 把 PC 上的 Live2D 模型同步到手机本地缓存。
 *
 * 为什么不把模型打进 APK：模型是第三方作品，说明文件明确写着
 * **「不可二传二改」**。打进 APK 等于再分发。所以模型只在运行时从你自己的 PC
 * 拉到你手机的私有目录，PC 与手机之间传，不经过任何第三方。
 *
 * 为什么要比对 sha1 而不是「文件在就跳过」：下载中断会留下**半截文件**，
 * 只判断存在会把坏文件当好的，等到渲染时才发现缺贴图 —— 那时候报的错
 * 会离真正的原因很远。所以按 manifest 的大小 + 哈希逐个校验。
 */
class ModelSync(private val context: Context) {

    data class Progress(
        val done: Int,
        val total: Int,
        val current: String,
        val bytesDone: Long,
        val bytesTotal: Long,
    ) {
        val percent: Int get() = if (bytesTotal <= 0) 0 else ((bytesDone * 100) / bytesTotal).toInt()
    }

    sealed interface Result {
        /** 全部就绪（含本来就在本地的情况） */
        data class Ready(val dir: File, val downloaded: Int, val skipped: Int) : Result
        data class Failed(val message: String) : Result
    }

    private val client = OkHttpClient.Builder()
        .connectTimeout(8, TimeUnit.SECONDS)
        .readTimeout(60, TimeUnit.SECONDS)
        .build()

    /** 模型缓存目录：优先内部私有目录（外部私有目录用于 adb push 联调） */
    fun modelDir(): File {
        val external = context.getExternalFilesDir(null)
        // 内部目录里已经有内容就用内部的，否则用外部私有目录（方便开发期 adb push）
        val internal = File(context.filesDir, DIR_NAME)
        if (internal.isDirectory && (internal.listFiles()?.isNotEmpty() == true)) return internal
        if (external != null) {
            val ext = File(external, DIR_NAME)
            if (ext.isDirectory && (ext.listFiles()?.isNotEmpty() == true)) return ext
        }
        return internal
    }

    /**
     * 同步。必须在后台线程调用（会做网络与大文件 IO）。
     *
     * @param onProgress 进度回调，可能被高频调用，界面侧自行节流
     */
    fun sync(host: String, port: Int, onProgress: (Progress) -> Unit = {}): Result {
        val base = "http://$host:$port"
        val dir = File(context.filesDir, DIR_NAME).apply { mkdirs() }
        val manifest = try {
            fetchManifest("$base/model/manifest")
        } catch (e: Exception) {
            // 网络抖一下就不渲染是不可接受的 —— 旧版 phone.html 正是把
            // 「模型可用」和「网络可用」绑死，才会一失败就整站不可用。
            // 本地已经有完整模型时，直接用缓存继续。
            if (File(dir, "miku.model3.json").isFile) {
                Log.w(TAG, "取清单失败，但本地已有模型，改用缓存：${e.message}")
                return Result.Ready(dir, 0, -1)
            }
            return Result.Failed("取模型清单失败：${e.message}")
        }
        if (manifest.isEmpty()) return Result.Failed("模型清单是空的（PC 的 REMOTE_MODEL_DIR 没配好？）")

        // 只按「相对路径 + 大小」预筛，真正下完再用 sha1 复核
        val need = manifest.filter { (rel, meta) ->
            val f = File(dir, rel)
            !f.isFile || f.length() != meta.second
        }
        val bytesTotal = manifest.values.sumOf { it.second }
        // 注意 need 是 Map，而 sumOf 定义在 Iterable 上 —— Map 不是 Iterable，
        // 必须显式取 .values，否则 "Unresolved reference 'sumOf'"。
        var bytesDone = bytesTotal - need.values.sumOf { it.second }
        var downloaded = 0
        val skipped = manifest.size - need.size

        Log.i(TAG, "模型清单 ${manifest.size} 个文件 / ${bytesTotal / 1024 / 1024}MB，需要下载 ${need.size} 个")

        for ((rel, meta) in need) {
            val size = meta.second
            val target = File(dir, rel)
            target.parentFile?.mkdirs()

            val tmp = File(target.parentFile, target.name + ".part")
            try {
                downloadTo("$base/model/$rel", tmp)
            } catch (e: Exception) {
                tmp.delete()
                return Result.Failed("下载 $rel 失败：${e.message}")
            }

            // 大小与哈希都要对 —— 半截文件最常见的表现就是大小对不上
            if (tmp.length() != size) {
                tmp.delete()
                return Result.Failed("$rel 大小不符（期望 $size，实得 ${tmp.length()}）")
            }
            val actual = sha1(tmp)
            if (actual != meta.first) {
                tmp.delete()
                return Result.Failed("$rel 校验失败（sha1 不符）")
            }
            if (!tmp.renameTo(target)) {
                target.delete()
                if (!tmp.renameTo(target)) {
                    tmp.delete()
                    return Result.Failed("$rel 落盘失败")
                }
            }
            downloaded++
            bytesDone += size
            onProgress(Progress(downloaded + skipped, manifest.size, rel, bytesDone, bytesTotal))
        }

        // 清理清单里已不存在的旧文件，避免换模型后残留
        prune(dir, manifest.keys)
        return Result.Ready(dir, downloaded, skipped)
    }

    private fun fetchManifest(url: String): Map<String, Pair<String, Long>> {
        val req = Request.Builder().url(url).build()
        client.newCall(req).execute().use { resp ->
            if (!resp.isSuccessful) throw IllegalStateException("HTTP ${resp.code}")
            val body = resp.body?.string() ?: throw IllegalStateException("空响应")
            val root = JSONObject(body)
            val files = root.optJSONArray("files") ?: return emptyMap()
            val out = LinkedHashMap<String, Pair<String, Long>>()
            for (i in 0 until files.length()) {
                val o = files.getJSONObject(i)
                out[o.getString("path")] = o.getString("sha1") to o.getLong("size")
            }
            return out
        }
    }

    private fun downloadTo(url: String, dest: File) {
        val req = Request.Builder().url(url).build()
        client.newCall(req).execute().use { resp ->
            if (!resp.isSuccessful) throw IllegalStateException("HTTP ${resp.code}")
            val body = resp.body ?: throw IllegalStateException("空 body")
            dest.outputStream().use { out -> body.byteStream().copyTo(out, 1 shl 16) }
        }
    }

    private fun sha1(f: File): String {
        val md = MessageDigest.getInstance("SHA-1")
        f.inputStream().use { ins ->
            val buf = ByteArray(1 shl 20)
            while (true) {
                val n = ins.read(buf)
                if (n <= 0) break
                md.update(buf, 0, n)
            }
        }
        return md.digest().joinToString("") { "%02x".format(it) }
    }

    private fun prune(dir: File, keep: Set<String>) {
        val keepFiles = keep.map { it.replace('/', File.separatorChar) }.toSet()
        dir.walkTopDown().filter { it.isFile && it.name != "使用说明.txt" }.forEach { f ->
            val rel = f.relativeTo(dir).path.replace(File.separatorChar, '/')
            if (rel !in keep) {
                Log.i(TAG, "清理多余文件 $rel")
                f.delete()
            }
        }
    }

    companion object {
        private const val TAG = "ModelSync"

        /** 与 PC 端 REMOTE_MODEL_DIR 的 basename 保持一致 */
        const val DIR_NAME = "miku_v5"
    }
}
