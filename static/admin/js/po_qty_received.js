/* po_qty_received.js — enable/disable qty_received inline inputs based on PO status */
(function(){
  var EDITABLE_STATUSES = ['partial', 'received'];

  function setQtyReceivedState(editable) {
    document.querySelectorAll('[name$="-qty_received"]').forEach(function(inp) {
      inp.disabled = !editable;
      inp.style.opacity = editable ? '1' : '0.45';
      inp.style.cursor  = editable ? '' : 'not-allowed';
      inp.title = editable
        ? ''
        : 'Доступно тільки при статусі «Частково отримано» або «Отримано»';
    });
  }

  document.addEventListener('DOMContentLoaded', function(){
    var statusSel = document.getElementById('id_status');
    if (!statusSel) return;

    /* Initial state */
    setQtyReceivedState(EDITABLE_STATUSES.indexOf(statusSel.value) !== -1);

    /* React immediately when status changes */
    statusSel.addEventListener('change', function(){
      setQtyReceivedState(EDITABLE_STATUSES.indexOf(statusSel.value) !== -1);
    });

    /* Handle dynamically added inline rows (Add another line) */
    var inlineGroup = document.querySelector('.inline-group');
    if (inlineGroup && window.MutationObserver) {
      new MutationObserver(function(){
        setQtyReceivedState(EDITABLE_STATUSES.indexOf(statusSel.value) !== -1);
      }).observe(inlineGroup, {childList: true, subtree: true});
    }
  });
})();
