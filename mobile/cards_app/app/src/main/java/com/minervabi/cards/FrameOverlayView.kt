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
 * Рамка видошукача: поза рамкою — трохи темніше, сама рамка — кути. Під панелями (topInset / bottomInset — ставить
 * екран камери за справжніми розмірами панелей) рамка не заходить, тож кнопки її не перекривають. Режими: card
 * (візитка 1,586 : 1), flyer (A4 стоячи), qr (квадрат), free (весь кадр — без рамки).
 * 📐 quad — знайдені краї візитки (px цього view): зелений контур поверх.
 */
class FrameOverlayView @JvmOverloads constructor(context: Context, attrs: AttributeSet? = null) : View(context, attrs) {
    var mode: String = "card"
        set(v) { field = v; invalidate() }
    var found: Boolean = false
        set(v) { if (field != v) { field = v; invalidate() } }
    var quad: FloatArray? = null
        set(v) { field = v; invalidate() }
    var topInset = context.dp(64)
        set(v) { if (field != v) { field = v; invalidate() } }
    var bottomInset = context.dp(230)
        set(v) { if (field != v) { field = v; invalidate() } }

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
    private val quadFill = Paint(Paint.ANTI_ALIAS_FLAG).apply { color = Color.argb(55, 63, 185, 80) }
    private val quadLine = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.STROKE
        strokeWidth = context.dp(3).toFloat()
        strokeJoin = Paint.Join.ROUND
        color = Color.rgb(63, 185, 80)
    }
    private val dot = Paint(Paint.ANTI_ALIAS_FLAG).apply { color = Color.rgb(63, 185, 80) }

    /** Рамка в px цього view (так само, як PreviewView над ним); null — режим «весь кадр». */
    fun frame(): RectF? {
        if (mode == "free" || width == 0) return null
        val w = width.toFloat()
        val availTop = topInset.toFloat()
        val availH = (height - topInset - bottomInset).toFloat().coerceAtLeast(height * 0.35f)
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
        val q = quad
        val r = context.dp(12).toFloat()
        val path = Path().apply {
            fillType = Path.FillType.EVEN_ODD
            addRect(0f, 0f, width.toFloat(), height.toFloat(), Path.Direction.CW)
            addRoundRect(f, r, r, Path.Direction.CW)
        }
        canvas.drawPath(path, dim)
        canvas.drawRoundRect(f, r, r, thin)
        stroke.color = when {
            q != null -> Color.argb(120, 255, 255, 255)
            found -> Color.rgb(63, 185, 80)
            else -> Color.WHITE
        }
        val c = context.dp(28).toFloat()
        val p = Path().apply {
            moveTo(f.left, f.top + c); lineTo(f.left, f.top); lineTo(f.left + c, f.top)
            moveTo(f.right - c, f.top); lineTo(f.right, f.top); lineTo(f.right, f.top + c)
            moveTo(f.right, f.bottom - c); lineTo(f.right, f.bottom); lineTo(f.right - c, f.bottom)
            moveTo(f.left + c, f.bottom); lineTo(f.left, f.bottom); lineTo(f.left, f.bottom - c)
        }
        canvas.drawPath(p, stroke)
        if (q != null) {
            val qp = Path().apply {
                moveTo(q[0], q[1]); lineTo(q[2], q[3]); lineTo(q[4], q[5]); lineTo(q[6], q[7]); close()
            }
            canvas.drawPath(qp, quadFill)
            canvas.drawPath(qp, quadLine)
            val rr = context.dp(5).toFloat()
            for (k in 0 until 4) canvas.drawCircle(q[2 * k], q[2 * k + 1], rr, dot)
        }
    }
}
