/**
 * The editable code list (TASK-071).
 *
 * The rendering distinctions here are requirements from CLAUDE.md's shape
 * contract rather than presentation choices: a `comprehend-medical` entry is a
 * suggestion and must be visibly one, and `null` and `[]` are two different
 * facts about an extraction pass that must not be shown as one.
 */

import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { CodeList } from '../../../src/components/CodeList';
import { llmCode, suggestedCode } from '../../support/notes';

function renderList(codes = [llmCode, suggestedCode]) {
  const onAccept = vi.fn();
  const onRemove = vi.fn();
  const onAdd = vi.fn();
  render(
    <CodeList
      title="ICD-10 diagnoses"
      name="icd10"
      codes={codes}
      onAccept={onAccept}
      onRemove={onRemove}
      onAdd={onAdd}
    />,
  );
  return { onAccept, onRemove, onAdd };
}

describe('rendering codes', () => {
  it('marks a suggestion as one and offers to accept it', () => {
    renderList();

    expect(screen.getByTestId(`icd10-entry-${suggestedCode.code}`)).toHaveAttribute(
      'data-source',
      'comprehend-medical',
    );
    expect(screen.getByTestId(`icd10-accept-${suggestedCode.code}`)).toBeInTheDocument();
  });

  /** Only a suggestion can be accepted — the others are already documentation. */
  it('offers no acceptance for a code that is not a suggestion', () => {
    renderList();

    expect(screen.queryByTestId(`icd10-accept-${llmCode.code}`)).not.toBeInTheDocument();
  });

  it('distinguishes an unanswered list from an empty one', () => {
    const { rerender } = render(
      <CodeList
        title="t"
        name="icd10"
        codes={null}
        onAccept={vi.fn()}
        onRemove={vi.fn()}
        onAdd={vi.fn()}
      />,
    );
    expect(screen.getByTestId('icd10-unanswered')).toBeInTheDocument();

    rerender(
      <CodeList
        title="t"
        name="icd10"
        codes={[]}
        onAccept={vi.fn()}
        onRemove={vi.fn()}
        onAdd={vi.fn()}
      />,
    );
    expect(screen.getByTestId('icd10-empty')).toBeInTheDocument();
    expect(screen.queryByTestId('icd10-unanswered')).not.toBeInTheDocument();
  });
});

describe('editing the list', () => {
  it('reports an acceptance by the code accepted', () => {
    const { onAccept } = renderList();

    fireEvent.click(screen.getByTestId(`icd10-accept-${suggestedCode.code}`));

    expect(onAccept).toHaveBeenCalledWith(suggestedCode.code);
  });

  it('reports a removal by the code removed', () => {
    const { onRemove } = renderList();

    fireEvent.click(screen.getByTestId(`icd10-remove-${llmCode.code}`));

    expect(onRemove).toHaveBeenCalledWith(llmCode.code);
  });

  it('adds what was typed and clears the field', () => {
    const { onAdd } = renderList();
    const input = screen.getByTestId('icd10-add-input');

    fireEvent.change(input, { target: { value: 'M17.12' } });
    fireEvent.click(screen.getByTestId('icd10-add'));

    expect(onAdd).toHaveBeenCalledWith('M17.12');
    expect(input).toHaveValue('');
  });

  it('adds nothing for an empty entry', () => {
    const { onAdd } = renderList();

    fireEvent.change(screen.getByTestId('icd10-add-input'), { target: { value: '   ' } });
    fireEvent.click(screen.getByTestId('icd10-add'));

    expect(onAdd).not.toHaveBeenCalled();
  });
});
