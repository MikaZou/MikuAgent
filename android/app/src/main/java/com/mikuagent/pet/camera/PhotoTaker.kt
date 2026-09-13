package com.mikuagent.pet.camera

import android.content.Context
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.graphics.Matrix
import android.util.Base64
import android.util.Log
import androidx.camera.core.CameraSelector
import androidx.camera.core.ImageCapture
import androidx.camera.core.ImageCaptureException
import androidx.camera.core.ImageProxy
import androidx.camera.core.Preview
import androidx.camera.lifecycle.ProcessCameraProvider
import androidx.core.content.ContextCompat
import androidx.lifecycle.LifecycleOwner
import java.io.ByteArrayOutputStream
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors

/**
 * 拍照发给 Miku 看。
 *
 * 用「按需绑定 → 拍一张 → 立刻解绑」的方式，而不是常驻：
 * 常驻会让摄像头一直亮着（用户会看到指示灯的隐私问题），也白耗电。
 * PC 端视频对话那一侧也是同样思路 —— 只在需要的那一刻取一帧。
 *
 * 为什么不在页面里用 getUserMedia：`http://192.168.x.x` 不是安全上下文，
 * 浏览器里 navigator.mediaDevices 直接是 undefined。原生没这个限制。
 */
class PhotoTaker(
    private val context: Context,
    private val lifecycleOwner: LifecycleOwner,
) {

    private val executor: ExecutorService = Executors.newSingleThreadExecutor()
    private var provider: ProcessCameraProvider? = null
    private var imageCapture: ImageCapture? = null

    /** 目标最长边。照片只用来给模型看，不需要原图分辨率，压小能省带宽和 token。 */
    private val maxEdge = 640

    fun take(onResult: (base64Jpeg: String?, error: String?) -> Unit) {
        Log.i(TAG, "take() 开始")
        val future = ProcessCameraProvider.getInstance(context)
        future.addListener({
            val cam = try {
                future.get()
            } catch (e: Exception) {
                Log.e(TAG, "取相机失败", e)
                onResult(null, "取相机失败：${e.message}")
                return@addListener
            }
            provider = cam
            Log.i(TAG, "CameraProvider 就绪，可用相机数=${cam.availableCameraInfos.size}")

            val capture = ImageCapture.Builder()
                .setCaptureMode(ImageCapture.CAPTURE_MODE_MINIMIZE_LATENCY)
                .build()
            imageCapture = capture

            val selector = CameraSelector.Builder()
                .requireLensFacing(CameraSelector.LENS_FACING_FRONT)
                .build()

            try {
                cam.unbindAll()
                // 只绑 ImageCapture：CameraX 会自动补一个 MeteringRepeating 做 3A。
                // 之前多绑了一个没有 SurfaceProvider 的 Preview，属于没必要的风险。
                cam.bindToLifecycle(lifecycleOwner, selector, capture)
                Log.i(TAG, "已绑定前置相机，开始 takePicture")
            } catch (e: Exception) {
                Log.e(TAG, "绑定相机失败", e)
                release()
                onResult(null, "前置相机不可用：${e.message}")
                return@addListener
            }

            capture.takePicture(
                ContextCompat.getMainExecutor(context),
                object : ImageCapture.OnImageCapturedCallback() {
                    override fun onCaptureSuccess(image: ImageProxy) {
                        Log.i(TAG, "onCaptureSuccess 格式=${image.format} ${image.width}x${image.height}")
                        var b64: String? = null
                        var err: String? = null
                        try {
                            b64 = encode(image)
                            Log.i(TAG, "编码完成 ${b64.length / 1024} KB")
                        } catch (e: Exception) {
                            Log.e(TAG, "编码失败", e)
                            err = "照片编码失败：${e.message}"
                        } finally {
                            image.close()
                        }
                        release()
                        onResult(b64, err)
                    }

                    override fun onError(exc: ImageCaptureException) {
                        Log.e(TAG, "onError: ${exc.imageCaptureError} ${exc.message}", exc)
                        release()
                        onResult(null, "拍照失败：${exc.message}")
                    }
                },
            )
        }, ContextCompat.getMainExecutor(context))
    }

    /** 拍完立刻解绑，别让摄像头一直开着。 */
    fun release() {
        try {
            provider?.unbindAll()
        } catch (_: Exception) {
        }
        provider = null
        imageCapture = null
    }

    fun shutdown() {
        release()
        executor.shutdown()
    }

    private fun encode(image: ImageProxy): String {
        // ImageProxy 出来的是 JPEG 时直接拿字节；否则转 Bitmap 再压
        val bitmap = image.toBitmapSafe()
        val scaled = scaleDown(bitmap, maxEdge)
        val out = ByteArrayOutputStream()
        scaled.compress(Bitmap.CompressFormat.JPEG, 80, out)
        if (scaled != bitmap) bitmap.recycle()
        scaled.recycle()
        return Base64.encodeToString(out.toByteArray(), Base64.NO_WRAP)
    }

    private fun ImageProxy.toBitmapSafe(): Bitmap {
        // CameraX 在 JPEG 输出时 buffer 本身就是 JPEG
        val buffer = planes[0].buffer
        val bytes = ByteArray(buffer.remaining())
        buffer.get(bytes)
        BitmapFactory.decodeByteArray(bytes, 0, bytes.size)?.let { return it }
        // 兜底：转成位图（不同设备输出格式可能不同）
        return toBitmap()
    }

    private fun scaleDown(src: Bitmap, maxEdge: Int): Bitmap {
        val longest = maxOf(src.width, src.height)
        if (longest <= maxEdge) return src
        val ratio = maxEdge.toFloat() / longest
        val w = (src.width * ratio).toInt().coerceAtLeast(1)
        val h = (src.height * ratio).toInt().coerceAtLeast(1)
        return Bitmap.createScaledBitmap(src, w, h, true).also {
            // 前置摄像头通常是镜像的，翻回来让 Miku 看到的是"正常朝向"
            val m = Matrix().apply { preScale(-1f, 1f) }
            Bitmap.createBitmap(it, 0, 0, it.width, it.height, m, true)
        }
    }

    companion object {
        private const val TAG = "PhotoTaker"
    }
}
