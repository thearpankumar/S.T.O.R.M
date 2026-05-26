from pydantic import BaseModel


class SheetSpec(BaseModel):
    sheet_name: str
    subdomain: str
    num_enterprise_tools: int
    num_opensource_tools: int
    total_rows: int


class ExcelRowMap(BaseModel):
    subdomain: str
    sheet_name: str
    start_row: int
    end_row: int
