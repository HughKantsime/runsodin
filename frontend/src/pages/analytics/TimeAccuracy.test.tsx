import React from 'react'
import {afterEach,expect,it,vi} from 'vitest'
import {cleanup,render,screen} from '@testing-library/react'
import {TimeAccuracyChart} from './Analytics'
vi.mock('recharts',()=>({ResponsiveContainer:({children}:any)=><div>{children}</div>,BarChart:({children}:any)=><div>{children}</div>,Bar:()=>null,CartesianGrid:()=>null,XAxis:()=>null,YAxis:()=>null,Tooltip:()=>null,Legend:()=>null,LineChart:()=>null,Line:()=>null}))
afterEach(cleanup)
it('explains unknown estimates instead of presenting perfect historical accuracy',()=>{
 render(<TimeAccuracyChart data={{by_printer:[],total_jobs:0,jobs_with_unknown_estimate:3}} />)
 expect(screen.getByText('No completed jobs with a valid retained schedule interval')).toBeTruthy()
 expect(screen.getByText('3 jobs excluded because the schedule interval is missing or invalid.')).toBeTruthy()
})
it('labels the comparison as current retained schedule agreement',()=>{
 render(<TimeAccuracyChart data={{by_printer:[{name:'Synthetic printer',estimated_hours:2,actual_hours:3}],total_jobs:1,avg_accuracy_pct:66.7,jobs_with_unknown_estimate:2}} />)
 expect(screen.getByText(/66.7% schedule agreement/)).toBeTruthy()
 expect(screen.getByText(/Uses the currently retained schedule interval. 2 jobs excluded/)).toBeTruthy()
})
