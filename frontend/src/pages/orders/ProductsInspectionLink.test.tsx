import {render,screen,cleanup} from '@testing-library/react'
import {MemoryRouter} from 'react-router-dom'
import {afterEach,expect,it,vi} from 'vitest'
import Products from './Products'
vi.mock('../../api',()=>({products:{list:async()=>[]},models:{list:async()=>[]},consumables:{list:async()=>[]}}))
vi.mock('../../permissions',()=>({canDo:()=>false,getCurrentUser:()=>({role:'viewer'})}))
vi.mock('../../components/orders/ProductionBatches',()=>({default:({inspectJobId}:{inspectJobId?:number})=><p>Inspection {inspectJobId??'not requested'}</p>}))
vi.mock('../../components/orders/OperationsWorkbench',()=>({default:()=>null}))
vi.mock('../../components/orders/Finishing',()=>({default:()=>null}))
vi.mock('../../components/orders/AssortmentPolicy',()=>({default:()=>null}))
vi.mock('../../components/orders/SquareInventory',()=>({default:()=>null}))
afterEach(cleanup)
it('products query opens the requested inspection',async()=>{
 render(<MemoryRouter initialEntries={['/products?inspect=42']}><Products/></MemoryRouter>)
 await screen.findByText('Inspection 42')
})
it('invalid inspection identifiers are ignored',async()=>{
 render(<MemoryRouter initialEntries={['/products?inspect=bad']}><Products/></MemoryRouter>)
 await screen.findByText('Inspection not requested')
})
