from pydantic import BaseModel


class FoodItem(BaseModel):
    name: str
    search_queries: list[str] = []
    weight_g: float
    calories: float
    protein: float
    fat: float
    carbs: float


class MealResult(BaseModel):
    items: list[FoodItem]
    total_calories: float
    total_protein: float
    total_fat: float
    total_carbs: float
