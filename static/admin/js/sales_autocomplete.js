// static/admin/js/sales_autocomplete.js
(function($) {
    $(document).ready(function() {
        // Автозаповнення Product коли вводиться SKU RAW
        $('.field-sku_raw input').on('blur', function() {
            var $skuInput = $(this);
            var skuValue = $skuInput.val().trim();
            
            if (!skuValue) return;
            
            var $row = $skuInput.closest('tr');
            var $productSelect = $row.find('.field-product select');
            
            if (!$productSelect.length) return;
            
            // ШукаємоProduct з таким SKU
            $.ajax({
                url: '/admin/inventory/product/',
                data: { q: skuValue },
                success: function(data) {
                    // Парсимо відповідь і знаходимо точний збіг
                    var $options = $(data).find('select option');
                    var found = false;
                    
                    $options.each(function() {
                        var optionText = $(this).text();
                        if (optionText.includes(skuValue)) {
                            $productSelect.val($(this).val());
                            found = true;
                            return false;
                        }
                    });
                    
                    if (!found) {
                        alert('⚠️ Товар "' + skuValue + '" не знайдено на складі');
                    }
                }
            });
        });
        
        // Автообчислення total_price = qty × unit_price
        $('.field-qty input, .field-unit_price input').on('change', function() {
            var $row = $(this).closest('tr');
            var qty = parseFloat($row.find('.field-qty input').val()) || 0;
            var unit_price = parseFloat($row.find('.field-unit_price input').val()) || 0;
            var total = (qty * unit_price).toFixed(2);
            
            $row.find('.field-total_price input').val(total);
        });
    });
})(django.jQuery);
