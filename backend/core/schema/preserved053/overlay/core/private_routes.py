"""Private HTTP response and validation policy shared by intake routes."""
from fastapi import HTTPException
from fastapi.routing import APIRoute
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

PRIVATE={'Cache-Control':'private, no-store','Pragma':'no-cache'}

class PrivateRoute(APIRoute):
    invalid_detail="Invalid show-order details. Check required fields, dates and whole USD cents."
    def get_route_handler(self):
        original=super().get_route_handler()
        async def private(request):
            try:
                response=await original(request)
                response.headers.update(PRIVATE)
                return response
            except RequestValidationError:
                return JSONResponse({'detail':self.invalid_detail},status_code=422,headers=PRIVATE)
            except HTTPException as exc:
                return JSONResponse({'detail':exc.detail},status_code=exc.status_code,headers=PRIVATE)
        return private

