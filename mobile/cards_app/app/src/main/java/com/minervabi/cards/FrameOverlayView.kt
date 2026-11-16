package com.minervabi.cards

import android.content.Context
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Paint
import android.graphics.Path
import android.graphics.RectF
import android.util.AttributeSet
import android.view.View
import kotlin.math.min

/**
 * Рамка видошукача: поза рамкою — трохи темніше, сама рамка — кути. Під панелями (верх 60dp, низ ~190dp) рамка
 * не заходить, тож кнопки її не перекривають. Режими: card (візитка 1,586 : 1), flyer (A4 стоячи), qr (квадрат),
 * free (весь кадр — без рамки).
 */
class FrameOverlayView @JvmOverloads constructor(context: Context, attrs: AttributeSet? = null) : View(context, attrs) {
    var mode: String = "card"
        set(v) { field = v; invalidate() }
    var found: Boolean = false
        set(v) { if (field != v) { field = v; invalidate() } }
    var topInset = context.dp(64)
    var bottomInset = context.dp(200)

    private val dim = Paint().apply { color = Color.argb(110, 0, 0, 0) }
    private val stroke = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.STROKE
        strokeWidth = context.dp(4).toFloat()
        strokeCap = Paint.Cap.ROUND
    }
    private val thin = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.STROKE
        strokeWidth = context.dp(1).toFloat()
        color = Color.argb(150, 255, 255, 255)
    }

    /** Рамка в px цього view (так само, як PreviewView над ним); null — режим «весь кадр». */
    fun frame(): RectF? {
        if (mode == "free" || width == 0) return null
        val w = width.toFloat()
        val availTop = topInset.toFloat()
        val availH = (height - topInset - bottomInset).toFloat().coerceAtLeast(height * 0.4f)
        val margin = w * 0.06f
        val ratio = when (mode) {
            "flyer" -> 1 / 1.414f      // A4 стоячи: ширина / висота
            "qr" -> 1f
            else -> 1.586f             // візитка ISO 7810
        }
        var fw = w - 2 * margin
        var fh = fw / ratio
        if (mode == "qr") {
            fw = min(fw, w * 0.7f); fh = fw
        }
        if (fh > availH - 2 * margin) {
            fh = availH - 2 * margin; fw = fh * ratio
        }
        val left = (w - fw) / 2
        val top = availTop + (availH - fh) / 2
        return RectF(left, top, left + fw, top + fh)
    }

    override fun onDraw(canvas: Canvas) {
        val f = frame() ?: return
        val path = Path().apply {
            fillType = Path.FillType.EVEN_ODD
            addRect(0f, 0f, width.toFloat(), height.toFloat(), Path.Direction.CW)
            addRoundRect(f, context.dp(12).toFloat(), context.dp(12).toFloat(), Path.Direction.CW)
        }
        canvas.drawPath(path, dim)
        canvas.drawRoundRect(f, context.dp(12).toFloat(), context.dp(12).toFloat(), thin)
        stroke.color = if (found) Color.rgb(63, 185, 80) else Color.WHITE
        val c = context.dp(28).toFloat()
        val p = Path().apply {
            moveTo(f.left, f.top + c); lineTo(f.left, f.top); lineTo(f.left + c, f.top)
            moveTo(f.right - c, f.top); lineTo(f.right, f.top); lineTo(f.right, f.top + c)
            moveTo(f.right, f.bottom - c); lineTo(f.right, f.bottom); lineTo(f.right - c, f.bottom)
            moveTo(f.left + c, f.bottom); lineTo(f.left, f.bottom); lineTo(f.left, f.bottom - c)
        }
        canvas.drawPath(p, stroke)
    }
}
