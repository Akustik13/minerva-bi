package com.minervabi.cards

import kotlin.math.abs
import kotlin.math.atan2
import kotlin.math.hypot
import kotlin.math.max
import kotlin.math.min
import kotlin.math.sqrt

/**
 * 📐 Краї візитки / флаєра на кадрі — без бібліотек, щоб «скан» був у тій самій камері, що й фото.
 *
 * Сіре зображення (≤ ~400 px) → розмиття → перепади яскравості (Собель) → кандидати: світлі й темні області (поріг
 * Оцу; візитка зазвичай світліша або темніша за стіл / руку) і області різких перепадів → для кожного опукла
 * оболонка → найбільший вписаний чотирикутник → сторони уточнюються прямими по краю області (заокруглені кути
 * візитки не з'їдають кутів) → перевірка: опуклий, кути 45…135°, під кожною стороною справді є край, область
 * заповнює чотирикутник. Найкращий — за площею × «скільки краю під сторонами». Не знайшли — null (тоді — по рамці).
 *
 * Лише прості типи (IntArray / FloatArray) — перевіряється звичайним JUnit без Android.
 */
object DocDetect {

    /** Для перевірок: чому кандидат не підійшов. */
    var debug: ((String) -> Unit)? = null

    /** Сіре зображення: px — яскравість 0…255, рядок за рядком. */
    class Gray(val w: Int, val h: Int, val px: IntArray)

    /**
     * Чотирикутник у координатах [g]: x0,y0 — лівий верхній, далі правий верхній, правий нижній, лівий нижній.
     * roi — де шукати (l, t, r, b, r і b не включно); null — усе зображення. minFrac — найменша частка ділянки, яку
     * має займати знайдене (камера: візитку наводять на рамку, тож логотип чи QR усередині — не візитка).
     */
    fun detect(g: Gray, roi: IntArray? = null, minFrac: Double = 0.08): FloatArray? {
        val l = (roi?.get(0) ?: 0).coerceIn(0, g.w - 1)
        val t = (roi?.get(1) ?: 0).coerceIn(0, g.h - 1)
        val r = (roi?.get(2) ?: g.w).coerceIn(l + 1, g.w)
        val b = (roi?.get(3) ?: g.h).coerceIn(t + 1, g.h)
        val w = r - l
        val h = b - t
        if (w < 32 || h < 32) return null
        val src = IntArray(w * h) { i -> g.px[(t + i / w) * g.w + l + i % w] }
        val blur = boxBlur(boxBlur(src, w, h), w, h)
        val gx = IntArray(w * h); val gy = IntArray(w * h)
        val mag = sobel(blur, w, h, gx, gy)
        // поріг «тут край»: не нижче шуму, але й не вище за край світлої візитки на сірому столі (≈ 16…25)
        val edge = percentile(mag, 0.75).coerceIn(10, 24)
        val ctx = Ctx(w, h, gx, gy, edge, minFrac)
        val minArea = (w * h * 0.06).toInt()

        var best: FloatArray? = null
        var bestScore = 0.0
        fun consider(mask: BooleanArray, byBox: Boolean = false) {
            for (comp in components(mask, w, h, minArea, byBox)) {
                val q = quadOf(comp) ?: continue
                val s = score(q, comp, ctx) ?: continue
                if (s > bestScore) { bestScore = s; best = q }
            }
        }
        // кілька порогів яскравості: візитка світліша / темніша за стіл, текст на ній — ще темніший
        val (t1, t2) = otsu2(blur)
        val levels = listOf(otsu(blur), t1, t2).sorted()
            .fold(mutableListOf<Int>()) { acc, v -> if (acc.none { abs(it - v) < 8 }) acc.add(v); acc }
        for (th in levels) {
            consider(erode(BooleanArray(w * h) { blur[it] > th }, w, h))
            consider(erode(BooleanArray(w * h) { blur[it] <= th }, w, h))
        }
        val byLevel = best
        // контур перепадів: білий папір на білому столі (яскравості майже ті самі) і двоколірна візитка (темна
        // шапка — за яскравістю знайшлася б лише світла половина). Контур ширший за сам край (розмиття +
        // розширення) — на 2 px усередину; перемагає, лише якщо помітно більший за знайдене за яскравістю
        best = null; bestScore = 0.0
        val strong = percentile(mag, 0.88).coerceIn(edge * 3 / 2, 40)
        consider(dilate(BooleanArray(w * h) { mag[it] >= strong }, w, h), byBox = true)
        val byEdge = best?.let { shrink(it, 2f) }
        best = when {
            byEdge == null -> byLevel
            byLevel == null || area(byEdge) > area(byLevel) * 1.25 -> byEdge
            else -> byLevel
        }

        val q = best ?: return null
        for (i in 0 until 4) { q[2 * i] += l; q[2 * i + 1] += t }
        return q
    }

    // ---------------------------------------------------------------- підготовка

    private fun boxBlur(a: IntArray, w: Int, h: Int): IntArray {
        val out = IntArray(a.size)
        for (y in 0 until h) {
            val y0 = max(0, y - 1); val y1 = min(h - 1, y + 1)
            for (x in 0 until w) {
                val x0 = max(0, x - 1); val x1 = min(w - 1, x + 1)
                var s = 0; var n = 0
                for (yy in y0..y1) { val row = yy * w; for (xx in x0..x1) { s += a[row + xx]; n++ } }
                out[y * w + x] = s / n
            }
        }
        return out
    }

    /** (|gx| + |gy|) / 4 (Собель), gx / gy — самі перепади; край кадру — 0. */
    private fun sobel(a: IntArray, w: Int, h: Int, ox: IntArray, oy: IntArray): IntArray {
        val m = IntArray(a.size)
        for (y in 1 until h - 1) {
            val r0 = (y - 1) * w; val r1 = y * w; val r2 = (y + 1) * w
            for (x in 1 until w - 1) {
                val gx = (a[r0 + x + 1] + 2 * a[r1 + x + 1] + a[r2 + x + 1]) - (a[r0 + x - 1] + 2 * a[r1 + x - 1] + a[r2 + x - 1])
                val gy = (a[r2 + x - 1] + 2 * a[r2 + x] + a[r2 + x + 1]) - (a[r0 + x - 1] + 2 * a[r0 + x] + a[r0 + x + 1])
                m[r1 + x] = (abs(gx) + abs(gy)) / 4
                ox[r1 + x] = gx; oy[r1 + x] = gy
            }
        }
        return m
    }

    private fun percentile(a: IntArray, p: Double): Int {
        val hist = IntArray(1024)
        a.forEach { hist[it.coerceIn(0, 1023)]++ }
        val need = (a.size * p).toLong()
        var acc = 0L
        for (v in hist.indices) { acc += hist[v]; if (acc >= need) return v }
        return 1023
    }

    private fun otsu(a: IntArray): Int {
        val hist = IntArray(256)
        a.forEach { hist[it.coerceIn(0, 255)]++ }
        val total = a.size.toDouble()
        var sum = 0.0
        for (i in 0..255) sum += i * hist[i].toDouble()
        var sumB = 0.0; var wB = 0.0; var best = 0.0; var th = 127
        for (i in 0..255) {
            wB += hist[i]
            if (wB == 0.0) continue
            val wF = total - wB
            if (wF == 0.0) break
            sumB += i * hist[i].toDouble()
            val mB = sumB / wB; val mF = (sum - sumB) / wF
            val v = wB * wF * (mB - mF) * (mB - mF)
            if (v > best) { best = v; th = i }
        }
        return th
    }

    /** Два пороги (Оцу на три класи): фон, візитка, текст. */
    private fun otsu2(a: IntArray): Pair<Int, Int> {
        val hist = DoubleArray(64)
        a.forEach { hist[it.coerceIn(0, 255) / 4]++ }
        val p = DoubleArray(65); val m = DoubleArray(65)
        for (i in 0 until 64) { p[i + 1] = p[i] + hist[i]; m[i + 1] = m[i] + i * hist[i] }
        fun part(i: Int, j: Int): Double {          // [i, j)
            val wk = p[j] - p[i]
            if (wk <= 0) return 0.0
            val mk = m[j] - m[i]
            return mk * mk / wk
        }
        var best = -1.0; var b1 = 21; var b2 = 42
        for (i in 1 until 63) for (j in i + 1 until 64) {
            val v = part(0, i) + part(i, j) + part(j, 64)
            if (v > best) { best = v; b1 = i; b2 = j }
        }
        return b1 * 4 - 1 to b2 * 4 - 1
    }

    private fun erode(m: BooleanArray, w: Int, h: Int): BooleanArray = BooleanArray(m.size) { i ->
        val x = i % w; val y = i / w
        m[i] && x > 0 && y > 0 && x < w - 1 && y < h - 1 && m[i - 1] && m[i + 1] && m[i - w] && m[i + w]
    }

    private fun dilate(m: BooleanArray, w: Int, h: Int): BooleanArray = BooleanArray(m.size) { i ->
        val x = i % w; val y = i / w
        m[i] || (x > 0 && m[i - 1]) || (x < w - 1 && m[i + 1]) || (y > 0 && m[i - w]) || (y < h - 1 && m[i + w])
    }

    // ---------------------------------------------------------------- області

    /** Область: межа (крайні точки кожного рядка й стовпця), площа «від краю до краю», дотики до країв кадру. */
    private class Comp(val pts: FloatArray, val n: Int, val spanArea: Double, val touches: Int)

    private class Ctx(val w: Int, val h: Int, val gx: IntArray, val gy: IntArray, val edge: Int, val minFrac: Double)

    /** byBox — для областей перепадів (лише контур): розмір — за рамкою області, не за кількістю точок. */
    private fun components(mask: BooleanArray, w: Int, h: Int, minArea: Int, byBox: Boolean): List<Comp> {
        val label = IntArray(mask.size)
        val stack = IntArray(mask.size)
        val out = mutableListOf<Comp>()
        var next = 0
        for (start in mask.indices) {
            if (!mask[start] || label[start] != 0) continue
            next++
            var sp = 0
            stack[sp++] = start
            label[start] = next
            val members = ArrayList<Int>()
            while (sp > 0) {
                val i = stack[--sp]
                members.add(i)
                val x = i % w; val y = i / w
                if (x > 0 && mask[i - 1] && label[i - 1] == 0) { label[i - 1] = next; stack[sp++] = i - 1 }
                if (x < w - 1 && mask[i + 1] && label[i + 1] == 0) { label[i + 1] = next; stack[sp++] = i + 1 }
                if (y > 0 && mask[i - w] && label[i - w] == 0) { label[i - w] = next; stack[sp++] = i - w }
                if (y < h - 1 && mask[i + w] && label[i + w] == 0) { label[i + w] = next; stack[sp++] = i + w }
            }
            if (members.size < (if (byBox) 60 else minArea)) continue
            val rowMin = IntArray(h) { Int.MAX_VALUE }; val rowMax = IntArray(h) { -1 }
            val colMin = IntArray(w) { Int.MAX_VALUE }; val colMax = IntArray(w) { -1 }
            for (i in members) {
                val x = i % w; val y = i / w
                if (x < rowMin[y]) rowMin[y] = x; if (x > rowMax[y]) rowMax[y] = x
                if (y < colMin[x]) colMin[x] = y; if (y > colMax[x]) colMax[x] = y
            }
            val pts = FloatArray(4 * (w + h))
            var n = 0
            var span = 0.0
            var x0 = w; var x1 = -1; var y0 = h; var y1 = -1
            for (y in 0 until h) if (rowMax[y] >= 0) {
                pts[2 * n] = rowMin[y].toFloat(); pts[2 * n + 1] = y.toFloat(); n++
                pts[2 * n] = rowMax[y].toFloat(); pts[2 * n + 1] = y.toFloat(); n++
                span += rowMax[y] - rowMin[y] + 1
                x0 = min(x0, rowMin[y]); x1 = max(x1, rowMax[y]); y0 = min(y0, y); y1 = max(y1, y)
            }
            for (x in 0 until w) if (colMax[x] >= 0) {
                pts[2 * n] = x.toFloat(); pts[2 * n + 1] = colMin[x].toFloat(); n++
                pts[2 * n] = x.toFloat(); pts[2 * n + 1] = colMax[x].toFloat(); n++
            }
            if (byBox && (x1 - x0 + 1).toLong() * (y1 - y0 + 1) < minArea) continue
            // дотик до краю ділянки пошуку (ерозія відсуває на 1 px) — область, що виходить за кадр, це стіл / фон
            var touches = 0
            if (x0 <= 1) touches++; if (y0 <= 1) touches++; if (x1 >= w - 2) touches++; if (y1 >= h - 2) touches++
            out.add(Comp(pts, n, span, touches))
        }
        return out
    }

    // ---------------------------------------------------------------- чотирикутник

    private fun quadOf(c: Comp): FloatArray? {
        if (c.touches >= 2) return null
        val hull = hull(c.pts, c.n)
        if (hull.size < 8) return null
        val poly = reduce(hull, 10)
        val q = maxQuad(poly) ?: return null
        return refine(q, c) ?: q
    }

    /** Опукла оболонка (монотонний ланцюг), точки — пари x, y. */
    private fun hull(p: FloatArray, n: Int): FloatArray {
        val idx = (0 until n).sortedWith(compareBy<Int>({ p[2 * it] }, { p[2 * it + 1] }))
        val h = IntArray(2 * n + 2)
        var k = 0
        fun cross(o: Int, a: Int, b: Int) =
            (p[2 * a] - p[2 * o]) * (p[2 * b + 1] - p[2 * o + 1]) - (p[2 * a + 1] - p[2 * o + 1]) * (p[2 * b] - p[2 * o])
        for (i in idx) { while (k >= 2 && cross(h[k - 2], h[k - 1], i) <= 0) k--; h[k++] = i }
        val lower = k + 1
        for (j in idx.indices.reversed()) {
            val i = idx[j]
            while (k >= lower && cross(h[k - 2], h[k - 1], i) <= 0) k--
            h[k++] = i
        }
        val m = max(0, k - 1)
        return FloatArray(2 * m) { p[2 * h[it / 2] + it % 2] }
    }

    /** Прибирати вершину, без якої многокутник втрачає найменше площі, доки їх не лишиться [keep]. */
    private fun reduce(poly: FloatArray, keep: Int): FloatArray {
        val xs = ArrayList<Float>(); val ys = ArrayList<Float>()
        for (i in 0 until poly.size / 2) { xs.add(poly[2 * i]); ys.add(poly[2 * i + 1]) }
        while (xs.size > keep) {
            var best = -1; var bestLoss = Double.MAX_VALUE
            val n = xs.size
            for (i in 0 until n) {
                val a = (i + n - 1) % n; val c = (i + 1) % n
                val loss = abs(((xs[i] - xs[a]) * (ys[c] - ys[a]) - (ys[i] - ys[a]) * (xs[c] - xs[a])).toDouble())
                if (loss < bestLoss) { bestLoss = loss; best = i }
            }
            xs.removeAt(best); ys.removeAt(best)
        }
        return FloatArray(2 * xs.size) { if (it % 2 == 0) xs[it / 2] else ys[it / 2] }
    }

    private fun area(q: FloatArray): Double {
        var s = 0.0
        val n = q.size / 2
        for (i in 0 until n) {
            val j = (i + 1) % n
            s += q[2 * i].toDouble() * q[2 * j + 1] - q[2 * j].toDouble() * q[2 * i + 1]
        }
        return abs(s) / 2
    }

    /** Найбільший чотирикутник з вершинами многокутника (їх ≤ 10 — перебір). */
    private fun maxQuad(poly: FloatArray): FloatArray? {
        val n = poly.size / 2
        if (n < 4) return null
        var best: FloatArray? = null
        var bestA = 0.0
        val q = FloatArray(8)
        for (a in 0 until n) for (b in a + 1 until n) for (c in b + 1 until n) for (d in c + 1 until n) {
            intArrayOf(a, b, c, d).forEachIndexed { k, i -> q[2 * k] = poly[2 * i]; q[2 * k + 1] = poly[2 * i + 1] }
            val ar = area(q)
            if (ar > bestA) { bestA = ar; best = q.copyOf() }
        }
        return best?.let(::order)
    }

    /** Чотирикутник на [d] px менший (до центру). */
    private fun shrink(q: FloatArray, d: Float): FloatArray {
        val cx = (q[0] + q[2] + q[4] + q[6]) / 4; val cy = (q[1] + q[3] + q[5] + q[7]) / 4
        return FloatArray(8) { i ->
            val k = i / 2
            val dx = q[2 * k] - cx; val dy = q[2 * k + 1] - cy
            val len = hypot(dx.toDouble(), dy.toDouble()).toFloat().coerceAtLeast(1f)
            val f = ((len - d * 1.41f) / len).coerceAtLeast(0.5f)
            if (i % 2 == 0) cx + dx * f else cy + dy * f
        }
    }

    /** Порядок: лівий верхній, правий верхній, правий нижній, лівий нижній. */
    fun order(q: FloatArray): FloatArray {
        val cx = (q[0] + q[2] + q[4] + q[6]) / 4; val cy = (q[1] + q[3] + q[5] + q[7]) / 4
        val idx = (0 until 4).sortedBy { atan2((q[2 * it + 1] - cy).toDouble(), (q[2 * it] - cx).toDouble()) }
        val start = idx.indices.minByOrNull { q[2 * idx[it]] + q[2 * idx[it] + 1] } ?: 0
        return FloatArray(8) { val i = idx[(start + it / 2) % 4]; q[2 * i + it % 2] }
    }

    /**
     * Сторони — прямі по краю області (точки біля сторони, без кутів), кути — їх перетини. Заокруглений кут візитки
     * тоді не «зрізає» чотирикутник. Якщо щось не так — null (лишається чотирикутник з оболонки).
     */
    private fun refine(q: FloatArray, c: Comp): FloatArray? {
        val lines = Array(4) { DoubleArray(3) }       // a·x + b·y = d, (a, b) — одинична нормаль
        for (s in 0 until 4) {
            val x1 = q[2 * s]; val y1 = q[2 * s + 1]
            val x2 = q[(2 * s + 2) % 8]; val y2 = q[(2 * s + 3) % 8]
            val len = hypot((x2 - x1).toDouble(), (y2 - y1).toDouble())
            if (len < 8) return null
            val ux = (x2 - x1) / len; val uy = (y2 - y1) / len
            val tol = max(2.0, len * 0.03)
            var n = 0; var sx = 0.0; var sy = 0.0
            val px = DoubleArray(c.n); val py = DoubleArray(c.n)
            for (i in 0 until c.n) {
                val dx = c.pts[2 * i] - x1; val dy = c.pts[2 * i + 1] - y1
                val along = dx * ux + dy * uy
                val off = abs(-dx * uy + dy * ux)
                if (off <= tol && along > len * 0.12 && along < len * 0.88) {
                    px[n] = c.pts[2 * i].toDouble(); py[n] = c.pts[2 * i + 1].toDouble(); sx += px[n]; sy += py[n]; n++
                }
            }
            if (n < 6) return null
            val mx = sx / n; val my = sy / n
            var cxx = 0.0; var cxy = 0.0; var cyy = 0.0
            for (i in 0 until n) { val dx = px[i] - mx; val dy = py[i] - my; cxx += dx * dx; cxy += dx * dy; cyy += dy * dy }
            // головний напрям (власний вектор найбільшого власного значення)
            val tr = cxx + cyy; val det = cxx * cyy - cxy * cxy
            val l1 = tr / 2 + sqrt(max(0.0, tr * tr / 4 - det))
            var dx = cxy; var dy = l1 - cxx
            if (abs(dx) + abs(dy) < 1e-9) { dx = l1 - cyy; dy = cxy }
            if (abs(dx) + abs(dy) < 1e-9) { dx = ux.toDouble(); dy = uy.toDouble() }
            val dl = hypot(dx, dy)
            val a = -dy / dl; val bb = dx / dl
            lines[s][0] = a; lines[s][1] = bb; lines[s][2] = a * mx + bb * my
        }
        val out = FloatArray(8)
        for (k in 0 until 4) {
            val p = lines[(k + 3) % 4]; val n2 = lines[k]       // кут k — між стороною k-1 і k
            val det = p[0] * n2[1] - p[1] * n2[0]
            if (abs(det) < 1e-6) return null
            val x = (p[2] * n2[1] - p[1] * n2[2]) / det
            val y = (p[0] * n2[2] - p[2] * n2[0]) / det
            val shift = hypot(x - q[2 * k], y - q[2 * k + 1])
            val side = hypot((q[(2 * k + 2) % 8] - q[2 * k]).toDouble(), (q[(2 * k + 3) % 8] - q[2 * k + 1]).toDouble())
            if (shift > side * 0.15 + 4) return null
            out[2 * k] = x.toFloat(); out[2 * k + 1] = y.toFloat()
        }
        return out
    }

    // ---------------------------------------------------------------- перевірка

    /** Оцінка: площа × частка країв під сторонами; null — не схоже на візитку / аркуш. */
    private fun score(q: FloatArray, c: Comp, ctx: Ctx): Double? {
        val a = area(q)
        val roiArea = ctx.w.toDouble() * ctx.h
        if (a < roiArea * ctx.minFrac || a > roiArea * 0.985) return null
        for (k in 0 until 4) {
            if (q[2 * k] < -2 || q[2 * k + 1] < -2 || q[2 * k] > ctx.w + 1 || q[2 * k + 1] > ctx.h + 1) return null
            val ax = q[(2 * k + 6) % 8] - q[2 * k]; val ay = q[(2 * k + 7) % 8] - q[2 * k + 1]
            val bx = q[(2 * k + 2) % 8] - q[2 * k]; val by = q[(2 * k + 3) % 8] - q[2 * k + 1]
            val cos = (ax * bx + ay * by) / (hypot(ax.toDouble(), ay.toDouble()) * hypot(bx.toDouble(), by.toDouble()) + 1e-9)
            if (abs(cos) > 0.72) return null                  // кут поза 44…136°
            val cr = ax * by - ay * bx
            val cr0 = (q[6] - q[0]) * (q[3] - q[1]) - (q[7] - q[1]) * (q[2] - q[0])
            if (cr * cr0 < 0) return null                     // не опуклий
        }
        val fill = c.spanArea / a
        if (fill < 0.82 || fill > 1.15) { debug?.invoke("fill=%.2f".format(fill)); return null }
        var sum = 0.0; var worst = 1.0
        for (s in 0 until 4) {
            val sup = support(q, s, ctx)
            sum += sup; worst = min(worst, sup)
        }
        val mean = sum / 4
        debug?.invoke("q=${q.joinToString { "%.0f".format(it) }} fill=%.2f mean=%.2f worst=%.2f edge=${ctx.edge}".format(fill, mean, worst))
        if (worst < 0.5 || mean < 0.62) return null
        return a * mean * mean
    }

    /**
     * Яка частка точок сторони лежить на справжньому краї: поруч (±2 px) перепад упоперек сторони ≥ порогу. Перепад
     * уздовж сторони (рядок тексту, що перетинає межу ділянки пошуку) — не край.
     */
    private fun support(q: FloatArray, s: Int, ctx: Ctx): Double {
        val x1 = q[2 * s]; val y1 = q[2 * s + 1]; val x2 = q[(2 * s + 2) % 8]; val y2 = q[(2 * s + 3) % 8]
        val len = hypot((x2 - x1).toDouble(), (y2 - y1).toDouble())
        if (len < 1) return 0.0
        val nx = -(y2 - y1) / len; val ny = (x2 - x1) / len
        val samples = 40
        var hit = 0; var all = 0
        for (i in 0 until samples) {
            val tt = 0.08 + 0.84 * i / (samples - 1)
            val x = (x1 + (x2 - x1) * tt).toInt(); val y = (y1 + (y2 - y1) * tt).toInt()
            if (x < 0 || y < 0 || x >= ctx.w || y >= ctx.h) continue
            all++
            var found = false
            loop@ for (dy in -2..2) for (dx in -2..2) {
                val xx = x + dx; val yy = y + dy
                if (xx < 0 || yy < 0 || xx >= ctx.w || yy >= ctx.h) continue
                val k = yy * ctx.w + xx
                if (abs(ctx.gx[k] * nx + ctx.gy[k] * ny) / 4 >= ctx.edge) { found = true; break@loop }
            }
            if (found) hit++
        }
        return if (all < samples / 2) 0.0 else hit.toDouble() / all
    }
}
