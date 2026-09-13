import * as React from 'react'
import { Slot } from '@radix-ui/react-slot'
import { cva, type VariantProps } from 'class-variance-authority'
import { cn } from '@/lib/utils'

const buttonVariants = cva('cursor-pointer inline-flex items-center justify-center gap-1.5 whitespace-nowrap rounded-md text-[13px] font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/40 disabled:pointer-events-none disabled:opacity-50 [&_svg]:pointer-events-none [&_svg]:size-3.5 [&_svg]:shrink-0', {
    variants: {
        variant: {
            default: 'bg-primary text-primary-foreground shadow-[inset_0_1px_0_rgb(255_255_255/0.14)] hover:bg-primary-hover',
            dark: 'bg-foreground text-white hover:bg-foreground/85',
            destructive: 'bg-destructive text-white hover:bg-destructive/90',
            outline: 'border border-border-strong bg-background text-text shadow-[0_1px_2px_rgb(0_0_0/0.03)] hover:bg-muted',
            secondary: 'bg-muted text-foreground hover:bg-track/60',
            ghost: 'text-text hover:bg-muted hover:text-foreground',
            link: 'text-primary underline-offset-4 hover:underline',
        },
        size: {
            default: 'h-8 px-3',
            sm: 'h-7 px-2.5 text-[12.5px]',
            lg: 'h-9 px-4',
            icon: 'size-8',
        },
    },
    defaultVariants: {
        variant: 'default',
        size: 'default',
    },
})

export interface ButtonProps extends React.ButtonHTMLAttributes<HTMLButtonElement>, VariantProps<typeof buttonVariants> {
    asChild?: boolean
}

const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(({ className, variant, size, asChild = false, ...props }, ref) => {
    const Comp = asChild ? Slot : 'button'
    return (
        <Comp
            className={cn(buttonVariants({ variant, size, className }))}
            ref={ref}
            {...props}
        />
    )
})
Button.displayName = 'Button'

export { Button, buttonVariants }
