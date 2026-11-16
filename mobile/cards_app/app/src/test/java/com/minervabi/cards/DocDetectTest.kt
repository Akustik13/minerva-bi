package com.minervabi.cards

import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import kotlin.math.abs
import kotlin.math.hypot
import kotlin.math.sin
import kotlin.random.Random

/** 📐 Краї: синтетичні знімки — візитка з «текстом» у перспективі на столі з текстурою. */
class DocDetectTest {
    init { DocDetect.debug = { println(it) } }

    /** Гомографія 4 точки → 4 точки (розв'язок 8×8). */
    private fun homography(src: DoubleArray, dst: DoubleArray): DoubleArray {
        val a = Array(8) { DoubleArray(9) }
        for (i in 0 until 4) {
            val x = src[2 * i]; val y = src[2 * i + 1]; val u = dst[2 * i]; val v = dst[2 * i + 1]
            a[2 * i] = doubleArrayOf(x, y, 1.0, 0.0, 0.0, 0.0, -u * x, -u * y, u)
            a[2 * i + 1] = doubleArrayOf(0.0, 0.0, 0.0, x, y, 1.0, -v * x, -v * y, v)
        }
        for (c in 0 until 8) {
            val p = (c until 8).maxByOrNull { abs(a[it][c]) }!!
            val t = a[c]; a[c] = a[p]; a[p] = t
            for (r in 0 until 8) if (r != c) {
                val f = a[r][c] / a[c][c]
                for (k in c..8) a[r][k] -= f * a[c][k]
            }
        }
        return DoubleArray(9) { if (it == 8) 1.0 else a[it][8] / a[it][it] }
    }

    private fun apply(h: DoubleArray, x: Double, y: Double): Pair<Double, Double> {
        val w = h[6] * x + h[7] * y + h[8]
        return (h[0] * x + h[1] * y + h[2]) / w to (h[3] * x + h[4] * y + h[5]) / w
    }

    /**
     * Знімок w×h: стіл (bg), на ньому візитка 85×55 з кутами quad (лівий верхній, правий верхній…), колір картки
     * card, текст — темніший / світліший рядками.
     */
    private fun scene(w: Int, h: Int, quad: DoubleArray, card: Int, bg: (Int, Int) -> Int, seed: Int = 1,
                      header: Int? = null): DocDetect.Gray {
        val rect = doubleArrayOf(0.0, 0.0, 85.0, 0.0, 85.0, 55.0, 0.0, 55.0)
        val inv = homography(quad, rect)
        val rnd = Random(seed)
        val px = IntArray(w * h)
        for (y in 0 until h) for (x in 0 until w) {
            val (u, v) = apply(inv, x + 0.5, y + 0.5)
            val noise = rnd.nextInt(-6, 7)
            px[y * w + x] = if (u in 0.0..85.0 && v in 0.0..55.0) {
                val text = (v in 8.0..12.0 && u in 8.0..60.0) || (v in 18.0..20.5 && u in 8.0..45.0) ||
                    (v in 24.0..26.5 && u in 8.0..52.0) || (v in 34.0..50.0 && u in 60.0..78.0)
                if (header != null && v < 22.0) header + noise
                else (if (text) (if (card > 128) 40 else 220) else card) + noise
            } else bg(x, y) + noise
        }
        for (i in px.indices) px[i] = px[i].coerceIn(0, 255)
        return DocDetect.Gray(w, h, px)
    }

    private fun assertNear(found: FloatArray?, truth: DoubleArray, tol: Double) {
        assertNotNull("краї не знайдено", found)
        val f = found!!
        for (k in 0 until 4) {
            val d = hypot(f[2 * k] - truth[2 * k], f[2 * k + 1] - truth[2 * k + 1])
            assertTrue("кут $k: ${f[2 * k]},${f[2 * k + 1]} проти ${truth[2 * k]},${truth[2 * k + 1]} (≈ $d px)", d <= tol)
        }
    }

    private val wood: (Int, Int) -> Int = { x, y -> (95 + 25 * sin(x / 6.0 + y / 21.0) + 10 * sin(y / 3.0)).toInt() }

    @Test
    fun whiteCardOnWoodInPerspective() {
        val q = doubleArrayOf(70.0, 120.0, 330.0, 95.0, 345.0, 280.0, 60.0, 300.0)
        assertNear(DocDetect.detect(scene(400, 420, q, 238, wood)), q, 6.0)
    }

    @Test
    fun rotatedCard() {
        val q = doubleArrayOf(120.0, 60.0, 330.0, 150.0, 260.0, 310.0, 50.0, 220.0)
        assertNear(DocDetect.detect(scene(400, 400, q, 235, wood)), q, 6.0)
    }

    @Test
    fun darkCardOnLightTable() {
        val q = doubleArrayOf(60.0, 100.0, 320.0, 110.0, 315.0, 270.0, 55.0, 262.0)
        assertNear(DocDetect.detect(scene(380, 380, q, 45, { _, _ -> 205 })), q, 6.0)
    }

    @Test
    fun whiteCardOnWhiteishTableWithShadowEdge() {
        // майже білий стіл: різниця яскравостей лише ~35
        val q = doubleArrayOf(50.0, 80.0, 300.0, 80.0, 300.0, 240.0, 50.0, 240.0)
        assertNear(DocDetect.detect(scene(360, 330, q, 240, { _, _ -> 200 })), q, 6.0)
    }

    @Test
    fun twoToneCardWithDarkHeader() {
        // темна шапка на третину візитки — за яскравістю видно лише світлу частину, контур — усю візитку
        val q = doubleArrayOf(70.0, 110.0, 330.0, 100.0, 338.0, 275.0, 64.0, 290.0)
        assertNear(DocDetect.detect(scene(400, 400, q, 236, wood, header = 35)), q, 7.0)
    }

    @Test
    fun smallLivePreviewFrame() {
        // кадр видошукача ≈ 180 px: візитка займає більшу частину
        val q = doubleArrayOf(22.0, 60.0, 160.0, 52.0, 166.0, 140.0, 18.0, 146.0)
        assertNear(DocDetect.detect(scene(184, 200, q, 232, wood, seed = 3)), q, 4.0)
    }

    @Test
    fun onlyInsideTheSearchArea() {
        val q = doubleArrayOf(100.0, 140.0, 300.0, 140.0, 300.0, 270.0, 100.0, 270.0)
        val g = scene(400, 420, q, 236, wood)
        assertNear(DocDetect.detect(g, intArrayOf(70, 110, 330, 300)), q, 6.0)
        // ділянка, що ріже візитку, — краї не вигадуються
        assertNull(DocDetect.detect(g, intArrayOf(150, 110, 380, 300)))
    }

    @Test
    fun nothingOnTexture() {
        val rnd = Random(7)
        val w = 300; val h = 300
        val px = IntArray(w * h) { i -> (wood(i % w, i / w) + rnd.nextInt(-20, 21)).coerceIn(0, 255) }
        assertNull(DocDetect.detect(DocDetect.Gray(w, h, px)))
    }

    @Test
    fun orderStartsTopLeftClockwise() {
        val o = DocDetect.order(floatArrayOf(10f, 90f, 90f, 10f, 10f, 10f, 90f, 90f))
        assertTrue(o.contentEquals(floatArrayOf(10f, 10f, 90f, 10f, 90f, 90f, 10f, 90f)))
    }
}
