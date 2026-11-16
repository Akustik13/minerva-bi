package com.minervabi.cards

import android.content.Context
import android.content.Intent
import android.content.res.ColorStateList
import android.graphics.Color
import android.net.Uri
import android.text.InputType
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.TextView
import android.widget.Toast
import androidx.core.content.ContextCompat
import com.google.android.material.button.MaterialButton
import com.google.android.material.chip.Chip
import com.google.android.material.chip.ChipGroup
import com.google.android.material.dialog.MaterialAlertDialogBuilder
import com.google.android.material.textfield.TextInputEditText
import com.google.android.material.textfield.TextInputLayout
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

fun Context.dp(v: Int): Int = (v * resources.displayMetrics.density).toInt()

fun Context.toast(text: String, long: Boolean = false) =
    Toast.makeText(this, text, if (long) Toast.LENGTH_LONG else Toast.LENGTH_SHORT).show()

fun Context.open(url: String) {
    runCatching { startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(url))) }.onFailure { toast("Немає чим відкрити: $url") }
}

fun Context.alert(title: String, text: String) {
    MaterialAlertDialogBuilder(this).setTitle(title).setMessage(text).setPositiveButton("OK", null).show()
}

fun Context.confirm(title: String, text: String, yes: String, action: () -> Unit) {
    MaterialAlertDialogBuilder(this).setTitle(title).setMessage(text)
        .setPositiveButton(yes) { _, _ -> action() }.setNegativeButton("Скасувати", null).show()
}

fun shortDate(ms: Long): String = SimpleDateFormat("dd.MM HH:mm", Locale.getDefault()).format(Date(ms))

/** Чипи щільніше: видима висота 40dp (зручно пальцем) без невидимих полів навколо. */
private fun Chip.compact() {
    setEnsureMinTouchTargetSize(false)
    chipMinHeight = context.dp(40).toFloat()
}

/** Вибране видно одразу: синій чип з білим текстом, невибране — лише контур. */
private fun Chip.selectedLook() {
    val accent = ContextCompat.getColor(context, R.color.accent_dark)
    val checked = intArrayOf(android.R.attr.state_checked)
    val text = textColors.defaultColor
    chipBackgroundColor = ColorStateList(arrayOf(checked, intArrayOf()), intArrayOf(accent, Color.TRANSPARENT))
    setTextColor(ColorStateList(arrayOf(checked, intArrayOf()), intArrayOf(Color.WHITE, text)))
    chipStrokeWidth = context.dp(1).toFloat()
    chipStrokeColor = ColorStateList(arrayOf(checked, intArrayOf()), intArrayOf(accent, (text and 0x00FFFFFF) or 0x66000000))
    checkedIconTint = ColorStateList.valueOf(Color.WHITE)
}

/** Чип-перемикач (позначки, категорії, фільтри). */
fun ChipGroup.addToggle(text: String, checked: Boolean, tag: Any? = null, onChange: ((Boolean) -> Unit)? = null): Chip {
    chipSpacingVertical = context.dp(6)
    val c = Chip(context).apply {
        compact()
        this.text = text
        isCheckable = true
        isChecked = checked
        this.tag = tag
        selectedLook()
        setOnCheckedChangeListener { _, on -> onChange?.invoke(on) }
    }
    addView(c)
    return c
}

fun ChipGroup.addAction(text: String, onClick: () -> Unit): Chip {
    chipSpacingVertical = context.dp(6)
    val c = Chip(context).apply {
        compact()
        this.text = text
        isCheckable = false
        setOnClickListener { onClick() }
    }
    addView(c)
    return c
}

fun ChipGroup.checkedTags(): List<Any> =
    (0 until childCount).mapNotNull { (getChildAt(it) as? Chip)?.takeIf { c -> c.isChecked }?.tag }

/** Поле для діалогу (з підписом). */
fun Context.field(hint: String, value: String = "", multi: Boolean = false): Pair<TextInputLayout, EditText> {
    val box = TextInputLayout(this).apply {
        this.hint = hint
        layoutParams = LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT)
            .apply { topMargin = dp(8) }
    }
    val et = TextInputEditText(box.context).apply {
        setText(value)
        inputType = if (multi) InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_FLAG_MULTI_LINE or
            InputType.TYPE_TEXT_FLAG_CAP_SENTENCES else InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_FLAG_CAP_SENTENCES
        if (multi) minLines = 2
    }
    box.addView(et)
    return box to et
}

fun Context.vbox(padding: Int = 20): LinearLayout = LinearLayout(this).apply {
    orientation = LinearLayout.VERTICAL
    setPadding(dp(padding), dp(8), dp(padding), 0)
}

fun Context.label(text: String, size: Float = 14f, secondary: Boolean = false): TextView = TextView(this).apply {
    this.text = text
    textSize = size
    if (secondary) alpha = 0.7f
    layoutParams = LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT)
        .apply { topMargin = dp(8) }
}

fun View.show(on: Boolean) {
    visibility = if (on) View.VISIBLE else View.GONE
}

/**
 * Кнопка зі стилем Material: tonal — звичайна дія, outlined — другорядна, text — тиха / небезпечна, filled — головна
 * (стиль задається лише розміткою — btn_*.xml).
 */
fun Context.button(kind: String, text: String, onClick: () -> Unit): MaterialButton {
    val res = when (kind) {
        "outlined" -> R.layout.btn_outlined
        "text" -> R.layout.btn_text
        "filled" -> R.layout.btn_filled
        else -> R.layout.btn_tonal
    }
    return (LayoutInflater.from(this).inflate(res, null, false) as MaterialButton).apply {
        this.text = text
        setOnClickListener { onClick() }
    }
}

/** Ряд кнопок однакової ширини (не виходить за край екрана). */
fun LinearLayout.addRowButton(btn: MaterialButton, gapStart: Boolean) {
    // однакова висота, навіть якщо текст однієї кнопки — у два рядки
    btn.layoutParams = LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.MATCH_PARENT, 1f)
        .apply { if (gapStart) marginStart = context.dp(8) }
    btn.maxLines = 2
    addView(btn)
}
