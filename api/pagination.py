from rest_framework.pagination import PageNumberPagination


class APIPagination(PageNumberPagination):
    """?page=N&page_size=M (до 500 записів на сторінку)."""
    page_size = 50
    page_size_query_param = "page_size"
    max_page_size = 500
