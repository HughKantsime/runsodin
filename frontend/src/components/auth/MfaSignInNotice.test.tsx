import { cleanup, render, screen } from '@testing-library/react'
import { BrowserRouter } from 'react-router-dom'
import { afterEach, expect, it } from 'vitest'
import MfaSignInNotice from './MfaSignInNotice'

afterEach(() => {
  cleanup()
  window.history.replaceState(null, '', '/')
})

it.each([0, 2, 4])('consumes the SSO history count of %i after reload', remaining => {
  window.history.replaceState({mfaCompletion: remaining}, '', '/')
  const {container} = render(<BrowserRouter><MfaSignInNotice /></BrowserRouter>)
  if (remaining === 4) {
    expect(container).toBeEmptyDOMElement()
  } else {
    expect(screen.getByRole('status')).toHaveTextContent(
      `You have ${remaining === 0 ? 'no' : remaining} recovery codes remaining. Generate a new set on the Account page.`
    )
  }
  expect(window.history.state?.mfaCompletion).toBeUndefined()
})
