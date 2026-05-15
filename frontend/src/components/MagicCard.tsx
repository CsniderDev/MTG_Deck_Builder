import React, { useState } from 'react';
import { createPortal } from 'react-dom';
import type { MagicCard, ScryfallImageUris } from '../types';
interface MagicCardProps {
  card: MagicCard;
  disableHover?: boolean; // Optional prop to disable hover preview if needed
}

const MagicCardItem: React.FC<MagicCardProps> = ({ card, disableHover }) => {
  const [isHovered, setIsHovered] = useState<boolean>(false);

  // Helper to handle DFCs (Double-Faced Cards) logic safely
  const getImageUrl = (size: keyof ScryfallImageUris): string => {
    if (card.image_uris) {
      return card.image_uris[size] || '';
    }
    // Fallback to the front face for DFCs
    return card.card_faces?.[0]?.image_uris?.[size] || '';
  };

  const normalImageUrl = getImageUrl('normal');
  const previewImageUrl = getImageUrl('large') || normalImageUrl;
  const shouldShowPreview = isHovered && previewImageUrl && !disableHover && typeof document !== 'undefined';

  return (
    <>
      <div
        className="card-container"
        onMouseEnter={() => setIsHovered(true)}
        onMouseLeave={() => setIsHovered(false)}
      >
        {normalImageUrl ? (
          <img
            src={normalImageUrl}
            alt={card.name}
            className="card-image"
            loading="lazy"
          />
        ) : null}
      </div>

      {shouldShowPreview
        ? createPortal(
            <div className="card-hover-preview" aria-hidden="true">
              <img src={previewImageUrl} alt={card.name} />
            </div>,
            document.body,
          )
        : null}
    </>
  );
};

export default MagicCardItem;