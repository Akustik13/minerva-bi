package com.minervabi.cards

import android.content.Context
import android.content.Intent
import android.net.Uri
import android.text.InputType
import android.view.View
import android.view.ViewGroup
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.TextView
import android.widget.Toast
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

/** Чип-перемикач (позначки, категорії, фільтри). */
/** Чипи щільніше: видима висота 40dp (зручно пальцем) без невидимих полів навколо. */
private fun Chip.compact() {
    setEnsureMinTouchTargetSize(false)
    chipMinHeight = context.dp(40).toFloat()
}

fun ChipGroup.addToggle(text: String, checked: Boolean, tag: Any? = null, onChange: ((Boolean) -> Unit)? = null): Chip {
    chipSpacingVertical = context.dp(6)
    val c = Chip(context).apply {
        compact()
        this.text = text
        isCheckable = true
        isChecked = checked
        this.tag = tag
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
