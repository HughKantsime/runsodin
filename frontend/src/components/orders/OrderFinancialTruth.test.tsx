import React from 'react'
import {afterEach,expect,it,vi} from 'vitest'
import {cleanup,render,screen} from '@testing-library/react'
import {OrderDetailModal} from './OrderModals'
afterEach(cleanup)
const base={id:1,order_number:'Synthetic financial truth',status:'pending',items:[],revenue:30,estimated_cost:12.99,actual_cost:null,profit:null,margin_percent:null}
function show(changes={}) {
 render(<OrderDetailModal isOpen order={{...base,...changes}} onClose={vi.fn()} onSchedule={vi.fn()} onShip={vi.fn()} onCancel={vi.fn()} onDownloadInvoice={vi.fn()} onConfirmProduction={vi.fn()} STATUS_CLASSES={{}} canDo={()=>false}/> )
}
function row(label:string){return screen.getByText(label).parentElement!}
it('shows estimated cost separately and unknown actual financials',()=>{
 show()
 expect(row('Estimated Cost')).toHaveTextContent('$12.99')
 for(const label of ['Actual Cost','Profit','Margin'])expect(row(label)).toHaveTextContent('Unknown')
})
it('preserves known zero cost profit and margin',()=>{
 show({actual_cost:0,profit:0,margin_percent:0})
 expect(row('Actual Cost')).toHaveTextContent('$0.00')
 expect(row('Profit')).toHaveTextContent('$0.00')
 expect(row('Margin')).toHaveTextContent('0%')
})
it('treats invalid actual financial values as unknown',()=>{
 show({actual_cost:Infinity,profit:NaN,margin_percent:Infinity})
 for(const label of ['Actual Cost','Profit','Margin'])expect(row(label)).toHaveTextContent('Unknown')
})
