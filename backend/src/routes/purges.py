from fastapi import APIRouter,Depends
from src.config.database import get_db
from src.utils.security import current_user,admin
from src.schemas.business import PurgeInput
from src.services import purges
router=APIRouter()
@router.get('/purges/preview')
def preview(lot:str='',db=Depends(get_db),user=Depends(current_user)):return purges.preview(db,lot)
@router.get('/purges')
def listing(db=Depends(get_db),user=Depends(current_user)):return [purges.serialize(j) for j in purges.listing(db)]
@router.post('/purges',status_code=201)
def create(data:PurgeInput,db=Depends(get_db),user=Depends(admin)):return purges.serialize(purges.create(db,data.lot,user))
@router.get('/purges/{id}')
def detail(id:int,db=Depends(get_db),user=Depends(current_user)):return purges.serialize(purges.get(db,id))
@router.post('/purges/{id}/retry')
def retry(id:int,db=Depends(get_db),user=Depends(admin)):return purges.serialize(purges.retry(db,id))
