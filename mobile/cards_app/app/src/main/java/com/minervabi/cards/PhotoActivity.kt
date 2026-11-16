package com.minervabi.cards

import android.content.Context
import android.content.Intent
import android.graphics.Matrix
import android.os.Bundle
import android.view.MotionEvent
import android.view.ScaleGestureDetector
import android.widget.ImageView
import androidx.appcompat.app.AppCompatActivity
import androidx.lifecycle.lifecycleScope

/** Фото на весь екран: свій знімок або візитка з Minerva; два пальці — збільшити, подвійний дотик — як було. */
class PhotoActivity : AppCompatActivity() {
    private val m = Matrix()
    private var scale = 1f

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val img = ImageView(this).apply { scaleType = ImageView.ScaleType.FIT_CENTER; setBackgroundColor(0xFF000000.toInt()) }
        setContentView(img)
        val file = intent.getStringExtra("file").orEmpty()
        val id = intent.getIntExtra("id", 0)
        if (file.isNotEmpty()) {
            img.setImageBitmap(ImageUtils.thumb(file, 2400))
        } else if (id > 0) {
            ImageLoader.server(this, lifecycleScope, img, id, intent.getStringExtra("side") ?: "front", full = true)
        }
        val zoom = ScaleGestureDetector(this, object : ScaleGestureDetector.SimpleOnScaleGestureListener() {
            override fun onScale(d: ScaleGestureDetector): Boolean {
                if (img.scaleType != ImageView.ScaleType.MATRIX) {
                    m.set(img.imageMatrix); img.scaleType = ImageView.ScaleType.MATRIX
                }
                val f = d.scaleFactor.coerceIn(0.5f, 2f)
                if (scale * f in 1f..8f) {
                    scale *= f
                    m.postScale(f, f, d.focusX, d.focusY)
                    img.imageMatrix = m
                }
                return true
            }
        })
        var lastTap = 0L
        var lx = 0f
        var ly = 0f
        img.setOnTouchListener { v, e ->
            zoom.onTouchEvent(e)
            when (e.actionMasked) {
                MotionEvent.ACTION_DOWN -> {
                    lx = e.x; ly = e.y
                    if (System.currentTimeMillis() - lastTap < 300) {
                        img.scaleType = ImageView.ScaleType.FIT_CENTER; scale = 1f; m.reset()
                    }
                    lastTap = System.currentTimeMillis()
                }
                MotionEvent.ACTION_MOVE -> if (!zoom.isInProgress && img.scaleType == ImageView.ScaleType.MATRIX && e.pointerCount == 1) {
                    m.postTranslate(e.x - lx, e.y - ly); img.imageMatrix = m; lx = e.x; ly = e.y
                }
                MotionEvent.ACTION_UP -> v.performClick()
            }
            true
        }
    }

    companion object {
        fun openFile(context: Context, file: String) {
            if (file.isNotEmpty()) context.startActivity(Intent(context, PhotoActivity::class.java).putExtra("file", file))
        }

        fun openServer(context: Context, id: Int, side: String) {
            context.startActivity(Intent(context, PhotoActivity::class.java).putExtra("id", id).putExtra("side", side))
        }
    }
}
