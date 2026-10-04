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
 *
 * 关于**两套模型**：每个模型有自己的目录 `files/models/<id>/`，互不干扰，
 * 所以换模型不需要重新下载全部内容（切回来时旧目录还在，直接命中缓存）。
 * 清单地址是 `/model/<id>/manifest`；同一个响应里还会带回 PC 的**渲染画像**
 * （情绪→表情、水印参数），手机端把它交给页面，两端行为就天然一致。
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
        data class Ready(
            val dir: File,
            val downloaded: Int,
            val skipped: Int,
            val modelId: String,
            /** PC 端给的渲染画像（JSON 原文），转发给 AssetServer 供页面读取 */
            val profileJson: String,
        ) : Result
        data class Failed(val message: String) : Result
    }

    private val client = OkHttpClient.Builder()
        .connectTimeout(8, TimeUnit.SECONDS)
        .readTimeout(60, TimeUnit.SECONDS)
        .build()

    /** 某个模型的缓存目录：优先内部私有目录（外部私有目录用于 adb push 联调） */
    fun modelDir(modelId: String): File {
        val internal = File(File(context.filesDir, AssetServerPaths.MODELS_ROOT), modelId)
        if (internal.isDirectory && internal.listFiles()?.isNotEmpty() == true) return internal
        val external = context.getExternalFilesDir(null)
            ?.let { File(File(it, AssetServerPaths.MODELS_ROOT), modelId) }
        if (external != null && external.isDirectory && external.listFiles()?.isNotEmpty() == true) {
            return external
        }
        return internal
    }

    /** 提交给 AssetServer 的目录（内部私有目录，ModelSync 负责创建）。 */
    private fun targetDir(modelId: String): File =
        File(File(context.filesDir, AssetServerPaths.MODELS_ROOT), modelId).apply { mkdirs() }

    /**
     * 清理旧版本留下的单模型缓存。
     *
     * 多模型之前，缓存直接放在 `files/miku_v5/`；现在统一放在
     * `files/models/<id>/`。不清理的话，升级 APK 的用户会白白多占
     * 30~40MB（模型贴图很大），而且没有任何提示 —— 这个目录已经不是
     * 任何代码会去读的路径，属于纯垃圾。
     */
    fun cleanupLegacyCache() {
        val legacy = File(context.filesDir, LEGACY_DIR_NAME)
        if (legacy.isDirectory) {
            val removed = legacy.deleteRecursively()
            Log.i(TAG, "清理旧版单模型缓存 ${legacy.name}：$removed")
        }
    }

    /**
     * 同步指定模型。必须在后台线程调用（会做网络与大文件 IO）。
     *
     * @param onProgress 进度回调，可能被高频调用，界面侧自行节流
     */
    fun sync(
        host: String,
        port: Int,
        modelId: String,
        onProgress: (Progress) -> Unit = {},
    ): Result {
        val base = "http://$host:$port"
        val dir = targetDir(modelId)
        val manifest = try {
            fetchManifest("$base/model/$modelId/manifest")
        } catch (e: Exception) {
            // 网络抖一下就不渲染是不可接受的 —— 旧版 phone.html 正是把
            // 「模型可用」和「网络可用」绑死，才会一失败就整站不可用。
            // 本地已经有完整模型时，直接用缓存继续。
            if (hasModelJson(dir)) {
                Log.w(TAG, "取清单失败，但本地已有 $modelId，改用缓存：${e.message}")
                return Result.Ready(dir, 0, -1, modelId, readCachedProfile(dir))
            }
            return Result.Failed("取模型清单失败：${e.message}")
        }
        if (manifest.files.isEmpty()) {
            return Result.Failed("模型清单是空的（PC 上没有这个模型？id=$modelId）")
        }

        // 只按「相对路径 + 大小」预筛，真正下完再用 sha1 复核
        val need = manifest.files.filter { (rel, meta) ->
            val f = File(dir, rel)
            !f.isFile || f.length() != meta.second
        }
        val bytesTotal = manifest.files.values.sumOf { it.second }
        // 注意 need 是 Map，而 sumOf 定义在 Iterable 上 —— Map 不是 Iterable，
        // 必须显式取 .values，否则 "Unresolved reference 'sumOf'"。
        var bytesDone = bytesTotal - need.values.sumOf { it.second }
        var downloaded = 0
        val skipped = manifest.files.size - need.size

        Log.i(TAG, "$modelId 清单 ${manifest.files.size} 个文件 / ${bytesTotal / 1024 / 1024}MB，" +
            "需要下载 ${need.size} 个")

        for ((rel, meta) in need) {
            val size = meta.second
            val target = File(dir, rel)
            target.parentFile?.mkdirs()

            // 临时文件名带上线程/时间戳，**不能**用固定的 `xxx.part`：
            // 曾经有两个同步并发跑，往同一个 .part 写，一个 rename 成功后
            // 另一个就「落盘失败」（真机上实测踩到，模拟器时序错开没撞上）。
            val tmp = File(
                target.parentFile,
                target.name + "." + Thread.currentThread().id + "." + System.nanoTime() + ".part",
            )
            try {
                downloadTo("$base/model/$modelId/$rel", tmp)
            } catch (e: Exception) {
                tmp.delete()
                return Result.Failed("下载 $rel 失败：${describe(e)}")
            }

            // 大小与哈希都要对 —— 半截文件最常见的表现就是大小对不上
            if (tmp.length() != size) {
                val got = tmp.length()
                tmp.delete()
                return Result.Failed("$rel 大小不符（期望 $size，实得 $got）")
            }
            val actual = sha1(tmp)
            if (actual != meta.first) {
                tmp.delete()
                return Result.Failed("$rel 校验失败（sha1 不符）")
            }
            // 用 Files.move(REPLACE_EXISTING) 而不是 File.renameTo：
            // renameTo 在目标已存在时行为不可靠（Android 上实测会失败）。
            if (!moveInto(tmp, target)) {
                tmp.delete()
                return Result.Failed("$rel 落盘失败（无法移动到 ${target.name}）")
            }
            downloaded++
            bytesDone += size
            onProgress(Progress(downloaded + skipped, manifest.files.size, rel, bytesDone, bytesTotal))
        }

        // 清理清单里已不存在的旧文件（同一模型换版本时残留的贴图/动作），
        // 但**不动别的模型目录** —— 那是另一套模型，切回去还要用。
        prune(dir, manifest.files.keys)
        // 画像落盘：PC 暂时连不上时（用缓存起页面）也还能用对情绪映射，
        // 否则「离线启动」会让 Miku 变成一张不会笑的脸。
        writeCachedProfile(dir, manifest.profile)
        return Result.Ready(dir, downloaded, skipped, modelId, manifest.profile)
    }

    /**
     * 外部（adb push）放模型时用的目录名。
     *
     * 单独抽出来而不是直接引用 AssetServer：ModelSync 在 model/ 包里，
     * 引 web/AssetServer 会把「同步」和「喂页面」耦合在一起，没必要。
     */
    private object AssetServerPaths {
        const val MODELS_ROOT = "models"
    }

    private class Manifest(
        val files: Map<String, Pair<String, Long>>,
        val profile: String,
    )

    private fun hasModelJson(dir: File): Boolean =
        dir.listFiles { f -> f.isFile && f.name.endsWith(".model3.json") }?.isNotEmpty() == true

    /** 画像缓存文件名。**不能**出现在 prune 的删除名单里。 */
    private fun profileFile(dir: File) = File(dir, PROFILE_FILE)

    private fun readCachedProfile(dir: File): String = try {
        val f = profileFile(dir)
        if (f.isFile) f.readText(Charsets.UTF_8) else "{}"
    } catch (e: Exception) {
        Log.w(TAG, "读画像缓存失败：${e.message}")
        "{}"
    }

    private fun writeCachedProfile(dir: File, profile: String) {
        if (profile.isBlank() || profile == "{}") return
        try {
            profileFile(dir).writeText(profile, Charsets.UTF_8)
        } catch (e: Exception) {
            Log.w(TAG, "写画像缓存失败：${e.message}")
        }
    }

    private fun fetchManifest(url: String): Manifest {
        val req = Request.Builder().url(url).build()
        client.newCall(req).execute().use { resp ->
            if (!resp.isSuccessful) throw IllegalStateException("HTTP ${resp.code}")
            val body = resp.body?.string() ?: throw IllegalStateException("空响应")
            if (body.trimStart().startsWith("{")) {
                // 404 时 PC 会返回 {"error": ...}，带着 404 的状态码；上面已经拦住了。
            }
            val root = JSONObject(body)
            val files = root.optJSONArray("files") ?: return Manifest(emptyMap(), "{}")
            val out = LinkedHashMap<String, Pair<String, Long>>()
            for (i in 0 until files.length()) {
                val o = files.getJSONObject(i)
                out[o.getString("path")] = o.getString("sha1") to o.getLong("size")
            }
            val profile = root.optJSONObject("profile")?.toString() ?: "{}"
            return Manifest(out, profile)
        }
    }

    /** 把临时文件搬到目标位置，覆盖已存在的目标。三条路依次尝试。 */
    private fun moveInto(tmp: File, target: File): Boolean {
        // 1) 原子替换（首选）
        try {
            java.nio.file.Files.move(
                tmp.toPath(), target.toPath(),
                java.nio.file.StandardCopyOption.REPLACE_EXISTING,
            )
            return true
        } catch (_: Exception) {
        }
        // 2) 先删目标再改名
        try {
            if (target.exists()) target.delete()
            if (tmp.renameTo(target)) return true
        } catch (_: Exception) {
        }
        // 3) 兜底：直接拷内容（慢但一定能成）
        return try {
            tmp.inputStream().use { ins ->
                target.outputStream().use { outs -> ins.copyTo(outs, 1 shl 16) }
            }
            tmp.delete()
            true
        } catch (_: Exception) {
            false
        }
    }

    private fun describe(e: Exception): String =
        e.message ?: e.javaClass.simpleName

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
        dir.walkTopDown().filter { it.isFile }.forEach { f ->
            val rel = f.relativeTo(dir).path.replace(File.separatorChar, '/')
            // 说明文件和「画像缓存」都不是清单里的文件，必须留着
            if (f.name == "使用说明.txt" || f.name == PROFILE_FILE) return@forEach
            if (rel !in keep) {
                Log.i(TAG, "清理多余文件 $rel")
                f.delete()
            }
        }
    }

    companion object {
        private const val TAG = "ModelSync"

        /** 画像缓存文件名，与 AssetServer 约定一致。 */
        private const val PROFILE_FILE = "_profile.json"

        /**
         * 旧版（多模型之前）的缓存目录名。
         *
         * 刻意写字面量而不是引用 AssetServer.DEFAULT_MODEL：那会让
         * model/ 包反向依赖 web/ 包，而 web/AssetServer 并不依赖 model。
         * 两边取值由 tools/test_android_consistency.py 校验一致。
         */
        private const val LEGACY_DIR_NAME = "miku_v5"
    }
}
