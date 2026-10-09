import React from 'react'
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import ReadinessPanel from './ReadinessPanel'
const state = vi.hoisted(() => ({ data: {} as any, error: false }))
vi.mock('../../api', () => ({ education: { readiness: vi.fn() } }))
vi.mock('@tanstack/react-query', () => ({ useQuery: () => ({ data: state.data, isError: state.error, isLoading: false }) }))
beforeEach(() => { state.error=false; state.data={ education_license:true,education_mode:true,oidc:{ready:true,provider:'google'}, classroom:{connected:false},pilot:{complete_centers:0,student_grants:3,manager_grants:2,printer_entitlements:1},storage:{status:'blocked',free_bytes:9*1024**3,total_bytes:45*1024**3,reserve_bytes:10*1024**3,upload_headroom_bytes:0,upload_directory_exists:true},backup:{database_backend:'sqlite',verified_workflow_available:true} } })
afterEach(cleanup)
it('shows measured reserve and administrator recovery, without hiding details', () => { render(<ReadinessPanel/>); const detail=screen.getByText(/9.00 GiB free of 45.00 GiB/); expect(detail).toHaveTextContent('10.00 GiB reserve'); expect(detail).toHaveTextContent('Free space or expand'); expect(detail.className).not.toContain('truncate') })
it('does not combine grants across different classes', () => { render(<ReadinessPanel/>); expect(screen.getByText(/same active class in Classes/)).toBeInTheDocument(); expect(screen.queryByText(/active class\(es\) have/)).not.toBeInTheDocument() })
it('shows complete configuration without claiming physical verification', () => { state.data.pilot.complete_centers=1;state.data.storage.status='ready';render(<ReadinessPanel/>);expect(screen.getByText(/1 active class\(es\)/)).toHaveTextContent('physical printing still need testing');expect(screen.getByText(/Headroom measured now/)).toBeInTheDocument() })
it('reports unknown storage and missing directory honestly', () => { state.data.storage.status='unknown'; const view=render(<ReadinessPanel/>);expect(screen.getByText(/Storage could not be measured/)).toBeInTheDocument();view.unmount();state.data.storage.status='ready';state.data.storage.upload_directory_exists=false;render(<ReadinessPanel/>);expect(screen.getByText(/measured its existing parent filesystem/)).toBeInTheDocument() })
it('keeps failed readiness separate from missing entitlement', () => {state.error=true;render(<ReadinessPanel/>);expect(screen.getByText('Readiness unavailable')).toBeInTheDocument()})
it('distinguishes invalid reserve configuration from missing disk data',()=>{state.data.storage.status='configuration_error';render(<ReadinessPanel/>);expect(screen.getByText(/Upload storage configuration is invalid/)).toHaveTextContent('Uploads are blocked');expect(screen.queryByText(/0.00 GiB free/)).not.toBeInTheDocument()})
it('identifies administrator override',()=>{state.data.storage.administrator_override=true;state.data.storage.configured_min_free_bytes=1024**3;render(<ReadinessPanel/>);expect(screen.getByText(/Administrator minimum: 1.00 GiB/)).toBeInTheDocument()})
