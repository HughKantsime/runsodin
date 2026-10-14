import React from 'react'
import { cleanup,fireEvent,render,screen,waitFor } from '@testing-library/react'
import { afterEach,beforeEach,expect,it,vi } from 'vitest'
import Upload from './VisionModelUpload'
import Settings from './VisionSettingsTab'
const mocks=vi.hoisted(()=>({upload:vi.fn(),models:vi.fn(),user:vi.fn()}))
vi.mock('../../api',()=>({vision:{uploadModel:mocks.upload,getModels:mocks.models,
 getSettings:vi.fn(async()=>({enabled:true,retention_days:30})),activateModel:vi.fn()},printers:{list:vi.fn(async()=>[])}}))
vi.mock('../../permissions',()=>({getCurrentUser:mocks.user}))
beforeEach(()=>{mocks.upload.mockReset();mocks.models.mockReset();mocks.user.mockReset();mocks.user.mockReturnValue({role:'admin',group_id:null});mocks.models.mockResolvedValue([])})
afterEach(cleanup)
it('sends explicitly selected YOLO class contract, including plate detection',async()=>{
 mocks.upload.mockResolvedValue({id:1});const uploaded=vi.fn();render(<Upload onUploaded={uploaded}/>);
 fireEvent.change(screen.getByLabelText('ONNX file'),{target:{files:[new File(['owned'],'plate.onnx')]}})
 fireEvent.change(screen.getByLabelText(/^Detection type/),{target:{value:'build_plate_empty'}})
 fireEvent.change(screen.getByLabelText(/^Output format/),{target:{value:'yolov8_class_scores'}})
 fireEvent.change(screen.getByLabelText(/^Output layout/),{target:{value:'rows_first'}})
 fireEvent.change(screen.getByLabelText('Class count'),{target:{value:'2'}})
 fireEvent.change(screen.getByLabelText('Positive class IDs'),{target:{value:'1'}})
 fireEvent.click(screen.getByRole('button',{name:'Upload Model'}))
 await waitFor(()=>expect(uploaded).toHaveBeenCalled())
 expect(mocks.upload).toHaveBeenCalledWith(expect.any(File),'plate','build_plate_empty',640,
  {version:1,format:'yolov8_class_scores',layout:'rows_first',class_count:2,positive_class_ids:[1]})
})
it('refuses duplicate class IDs before upload',()=>{
 render(<Upload onUploaded={vi.fn()}/>);
 fireEvent.change(screen.getByLabelText('ONNX file'),{target:{files:[new File(['owned'],'model.onnx')]}})
 fireEvent.change(screen.getByLabelText(/^Output format/),{target:{value:'yolov8_class_scores'}})
 fireEvent.change(screen.getByLabelText('Positive class IDs'),{target:{value:'0,0'}})
 fireEvent.click(screen.getByRole('button',{name:'Upload Model'}))
 expect(screen.getByRole('alert')).toHaveTextContent('Check input size')
 expect(mocks.upload).not.toHaveBeenCalled()
})
it('surfaces failed uploads',async()=>{
 mocks.upload.mockRejectedValue(new Error('Owned failure'));render(<Upload onUploaded={vi.fn()}/>);
 fireEvent.change(screen.getByLabelText('ONNX file'),{target:{files:[new File(['owned'],'model.onnx')]}})
 fireEvent.click(screen.getByRole('button',{name:'Upload Model'}))
 expect(await screen.findByRole('alert')).toHaveTextContent('Model upload failed')
})
it('does not present global model mutations to tenant admins',()=>{
 mocks.user.mockReturnValue({role:'admin',group_id:1});render(<Settings/>);
 expect(screen.queryByRole('button',{name:'Upload Model'})).not.toBeInTheDocument()
 expect(mocks.models).not.toHaveBeenCalled()
})
it('shows active model readiness as unknown and refreshes real reported state',async()=>{
 const model={id:1,name:'Owned model',detection_type:'spaghetti',is_active:1,runtime_status:{state:'unknown'}};
 mocks.models.mockResolvedValueOnce([model]).mockResolvedValueOnce([{...model,runtime_status:{state:'unavailable'}}]);render(<Settings/>);
 expect(await screen.findByText('Reported readiness: unknown')).toBeInTheDocument()
 fireEvent.click(screen.getByRole('button',{name:'Refresh model status'}))
 expect(await screen.findByText('Reported readiness: unavailable')).toBeInTheDocument()
})

it('refreshes after an unconfirmed registration and does not call it a definite failure',async()=>{
 mocks.upload.mockRejectedValue(Object.assign(new Error('Unconfirmed'),{status:503}));const refreshed=vi.fn();render(<Upload onUploaded={refreshed}/>);
 fireEvent.change(screen.getByLabelText('ONNX file'),{target:{files:[new File(['owned'],'model.onnx')]}})
 fireEvent.click(screen.getByRole('button',{name:'Upload Model'}))
 expect(await screen.findByRole('alert')).toHaveTextContent('registration is unconfirmed')
 expect(refreshed).toHaveBeenCalledOnce()
})
it('separates successful upload from a failed registry refresh',async()=>{
 mocks.upload.mockResolvedValue({id:1});render(<Upload onUploaded={vi.fn().mockRejectedValue(new Error('Refresh failed'))}/>);
 fireEvent.change(screen.getByLabelText('ONNX file'),{target:{files:[new File(['owned'],'model.onnx')]}})
 fireEvent.click(screen.getByRole('button',{name:'Upload Model'}))
 expect(await screen.findByRole('alert')).toHaveTextContent('Model uploaded, but')
 expect(mocks.upload).toHaveBeenCalledOnce()
})
it('admits only one overlapping upload',async()=>{
 let resolve; mocks.upload.mockReturnValue(new Promise(r=>{resolve=r}));render(<Upload onUploaded={vi.fn()}/>);
 fireEvent.change(screen.getByLabelText('ONNX file'),{target:{files:[new File(['owned'],'model.onnx')]}})
 const button=screen.getByRole('button',{name:'Upload Model'});fireEvent.click(button);fireEvent.click(button)
 expect(mocks.upload).toHaveBeenCalledOnce();resolve({id:1})
 await waitFor(()=>expect(button).toBeEnabled())
})

it('does not call a merely loaded model verified inference readiness',async()=>{
 mocks.models.mockResolvedValue([{id:1,name:'Owned',detection_type:'spaghetti',is_active:1,runtime_status:{state:'loaded'}}]);render(<Settings/>);
 expect(await screen.findByText('Reported readiness: loaded; inference not yet verified')).toBeInTheDocument()
})
